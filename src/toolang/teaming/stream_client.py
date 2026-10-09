"""Atomic multi-origin reduction using the local structural client reducer."""

from __future__ import annotations

from typing import Any

from toolang.execution.events import RunRetried, event_from_data
from toolang.execution.schemas import StreamFrame
from toolang.execution.stream_client import StreamClientState
from .errors import ForgottenTree
from .events import HubCursor, HubScope, MAX_BYTES, encode


class HubStreamState:
    def __init__(self, scope: HubScope) -> None:
        self.scope = scope
        self.cursor: str | None = None
        self.agents: dict[str, StreamClientState] = {}
        self.status: dict[str, dict[str, Any]] = {}
        self._prefix: list[StreamFrame] | None = None
        self._replacement: dict[str, Any] | None = None
        self._bytes = 0

    def attach(self) -> None:
        self._prefix = None
        self._replacement = None
        self._bytes = 0
        for state in self.agents.values():
            state.attach()

    @staticmethod
    def _apply(agents: dict[str, StreamClientState], frame: StreamFrame) -> None:
        agent = frame.data["agent"]
        local = agents.setdefault(agent, StreamClientState())
        data = dict(frame.data)
        cursor = data.pop("source_cursor", None)
        data.pop("agent")
        data.pop("cursor", None)
        if cursor is not None:
            data["cursor"] = cursor
        event = event_from_data(data)
        if isinstance(event, RunRetried) and not local.has_run(event.run):
            raise ForgottenTree("Retry needs a forgotten tree")
        local.feed(
            StreamFrame(frame.event, data, None if data.get("context") else cursor)
        )

    def feed(self, frame: StreamFrame) -> None:
        if frame.event == "stream_prefill":
            if (
                self._prefix is not None
                or frame.data.get("scope") != self.scope.data()
                or frame.id is not None
            ):
                raise ValueError("Invalid Hub prefill scope")
            HubCursor.parse(frame.data["cursor"])
            replacement = frame.data.get("replace")
            if replacement is not None and (
                not isinstance(replacement, list)
                or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("agent"), str)
                    or item.get("roots") is not None
                    and (
                        not isinstance(item["roots"], list)
                        or not all(isinstance(root, str) for root in item["roots"])
                    )
                    for item in replacement
                )
            ):
                raise ValueError("Invalid Hub replacement")
            self._replacement = frame.data
            self._prefix = []
            return
        if frame.event == "stream_checkpoint":
            cursor = HubCursor.parse(frame.data["cursor"])
            if frame.id != str(cursor):
                raise ValueError("Checkpoint ID mismatch")
            if self._prefix is not None:
                replacement = self._replacement
                if replacement is None or str(cursor) != replacement["cursor"]:
                    raise ValueError("Incomplete Hub prefix")
                agents = {name: state.copy() for name, state in self.agents.items()}
                statuses = dict(self.status)
                if replacement["replace"] is None:
                    agents.clear()
                    statuses.clear()
                else:
                    for item in replacement["replace"]:
                        if item["roots"] is None:
                            agents.pop(item["agent"], None)
                            statuses.pop(item["agent"], None)
                        elif item["agent"] in agents:
                            agents[item["agent"]].forget(set(item["roots"]))
                for item in self._prefix:
                    if item.event == "stream_status":
                        statuses[item.data["agent"]] = item.data
                    else:
                        self._apply(agents, item)
                self.agents, self.status = agents, statuses
                self._prefix = None
                self._replacement = None
                self._bytes = 0
            elif self.cursor is not None:
                previous = HubCursor.parse(self.cursor)
                if (
                    previous.epoch != cursor.epoch
                    or previous.position > cursor.position
                ):
                    raise ValueError("Hub checkpoint moved backwards or changed epoch")
            self.cursor = str(cursor)
            return
        if self._prefix is not None:
            if frame.id is not None or frame.event.startswith("part_"):
                raise ValueError("Hub prefill contains acknowledged progress")
            self._bytes += len(encode(frame.data).encode())
            if self._bytes > MAX_BYTES or len(self._prefix) >= 20000:
                raise ValueError("Hub prefill exceeds client budget")
            self._prefix.append(frame)
            return
        if frame.event == "stream_status":
            self.status[frame.data["agent"]] = frame.data
            return
        cursor = HubCursor.parse(frame.id) if frame.id is not None else None
        if cursor is not None and self.cursor is not None:
            previous = HubCursor.parse(self.cursor)
            if cursor.epoch != previous.epoch:
                raise ValueError("Hub epoch requires replacement")
            if cursor.position <= previous.position:
                return
        # A reducer error cannot advance the cursor or partially mutate a tree.
        agents = dict(self.agents)
        agent = frame.data["agent"]
        if agent in agents:
            agents[agent] = agents[agent].copy()
        self._apply(agents, frame)
        self.agents = agents
        if cursor is not None:
            self.cursor = str(cursor)
