"""Bounded activity reduction and terminal presentation for both stream transports."""

from __future__ import annotations

import asyncio
from contextlib import suppress, closing
from datetime import datetime, timezone
import json
import sys
import time

import httpx
from httpx_sse import aconnect_sse
from prompt_toolkit.input import create_input
from prompt_toolkit.keys import Keys
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from toolang.execution.events import (
    RunBegin,
    RunEnd,
    RunRetried,
    StepBegin,
    StepEnd,
    event_from_data,
)
from toolang.execution.schemas import StreamFrame
from toolang.execution.stream_client import StreamClientState
from toolang.teaming.events import HubScope
from toolang.teaming.errors import ForgottenTree
from toolang.teaming.stream_client import HubStreamState


class Activity:
    def __init__(self, agent: str | None) -> None:
        self.agent = agent
        self.hub = HubStreamState(HubScope()) if agent is None else None
        self.local = StreamClientState()
        self.initial = True
        self.reconnecting = False
        self.completed: dict[tuple[str, str], float] = {}

    @property
    def cursor(self) -> str | None:
        return self.hub.cursor if self.hub else self.local.cursor

    @property
    def agents(self) -> dict[str, StreamClientState]:
        return self.hub.agents if self.hub else {self.agent or "agent": self.local}

    def attach(self) -> None:
        if self.hub:
            self.hub.attach()
        else:
            self.local.attach()

    def feed(self, frame: StreamFrame) -> None:
        if self.hub:
            self.hub.feed(frame)
        else:
            if frame.event == "run_retried":
                event = event_from_data(frame.data)
                assert isinstance(event, RunRetried)
                if not self.local.has_run(event.run):
                    raise ForgottenTree("Retry needs a forgotten tree")
            self.local.feed(frame)
        if frame.event == "stream_checkpoint":
            self.prune()
            self.initial = False
        elif not self.initial:
            self.prune()

    def complete(self, agent: str, state: StreamClientState, root: str) -> bool:
        return state.complete(root) and (
            self.hub is None or self.hub.status.get(agent, {}).get("complete") is True
        )

    def prune(self) -> None:
        now = time.monotonic()
        done = set()
        for agent, state in self.agents.items():
            for event in state.snapshot().events:
                if (
                    isinstance(event, RunBegin)
                    and event.parent is None
                    and self.complete(agent, state, event.run)
                ):
                    key = (agent, event.run)
                    done.add(key)
                    self.completed.setdefault(key, now - 31 if self.initial else now)
        self.completed = {
            key: stamp for key, stamp in self.completed.items() if key in done
        }
        retained = set(
            sorted(self.completed, key=self.completed.__getitem__, reverse=True)[:20]
        )
        for key, stamp in tuple(self.completed.items()):
            if now - stamp >= 30 or key not in retained:
                self.agents[key[0]].forget({key[1]})
                self.completed.pop(key)

    def render(self) -> Group:
        self.prune()
        statuses = (
            self.hub.status
            if self.hub
            else {self.agent or "agent": dict(online=True, complete=True)}
        )
        summary = []
        for agent, status in sorted(statuses.items()):
            labels = ["online" if status["online"] else "offline"]
            if not status["complete"]:
                labels.append("incomplete")
            if self.reconnecting:
                labels.append("reconnecting")
            summary.append(f"{agent}: {', '.join(labels)}")
        table = Table(expand=True)
        for column in (
            "Agent",
            "Thread",
            "Run",
            "Runnable",
            "Status",
            "Step",
            "Elapsed",
        ):
            table.add_column(column, overflow="ellipsis")
        for agent, state in sorted(self.agents.items()):
            snapshot = state.snapshot().events
            roots = {}
            begins = {}
            ends = {}
            steps: dict[str, list[str]] = {}
            for event in snapshot:
                if isinstance(event, RunBegin):
                    roots[event.run] = (
                        roots.get(event.parent.run_id, event.parent.run_id)
                        if event.parent
                        else event.run
                    )
                    if event.parent is None:
                        begins[event.run] = event
                elif isinstance(event, RunEnd):
                    ends[event.run] = event
                elif isinstance(event, StepBegin):
                    steps.setdefault(
                        roots.get(event.step.run_id, event.step.run_id), []
                    ).append(str(event.step))
                elif isinstance(event, StepEnd):
                    active = steps.get(
                        roots.get(event.step.run_id, event.step.run_id), []
                    )
                    if str(event.step) in active:
                        active.remove(str(event.step))
            for root, begin in begins.items():
                end = ends.get(root)
                status = (
                    end.status
                    if end is not None and self.complete(agent, state, root)
                    else "running"
                    if begin.started_at
                    else "pending"
                )
                if self.hub is not None and not self.hub.status.get(agent, {}).get(
                    "complete", False
                ):
                    status = "incomplete"
                elapsed = "—"
                if begin.started_at:
                    start = datetime.fromisoformat(
                        begin.started_at.replace("Z", "+00:00")
                    )
                    finish = (
                        datetime.fromisoformat(end.finished_at.replace("Z", "+00:00"))
                        if end and state.complete(root)
                        else datetime.now(timezone.utc)
                    )
                    elapsed = f"{max(0, (finish - start).total_seconds()):.0f}s"
                active = steps.get(root, [])
                step = (
                    active[-1] + (f" (+{len(active) - 1})" if len(active) > 1 else "")
                    if active
                    else "—"
                )
                table.add_row(
                    *(
                        Text(value)
                        for value in (
                            agent,
                            begin.thread_id,
                            root,
                            begin.runnable,
                            status,
                            step,
                            elapsed,
                        )
                    )
                )
        if not summary:
            summary = ["No agents" if not self.reconnecting else "Reconnecting"]
        return Group(
            Text(" · ".join(summary)), table, Text("q / Ctrl-C quit", style="dim")
        )


async def watch(
    endpoint: str, *, agent: str | None, token: str | None, once: bool, console: Console
) -> None:
    state = Activity(agent)
    stop = asyncio.Event()

    async def receive():
        delay = 0.5
        async with httpx.AsyncClient(
            base_url=endpoint,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=httpx.Timeout(10, read=None),
            trust_env=False,
        ) as http:
            while not stop.is_set():
                state.attach()
                try:
                    async with aconnect_sse(
                        http,
                        "GET",
                        "/events/stream" if agent is None else "/api/v1/stream",
                        params={"after": state.cursor} if state.cursor else {},
                    ) as events:
                        if events.response.status_code in {400, 401, 403, 404, 422}:
                            raise ValueError(
                                f"Activity request rejected ({events.response.status_code}); check the running service"
                            )
                        if events.response.status_code == 503:
                            await events.response.aread()
                            data = events.response.json()
                            if data.get("code") in {"snapshot_limit", "protocol_error"}:
                                raise ValueError(
                                    f"Activity unavailable: {data['code']}"
                                )
                        events.response.raise_for_status()
                        state.reconnecting = False
                        async for event in events.aiter_sse():
                            if not event.data:
                                continue
                            frame = StreamFrame(
                                event.event, json.loads(event.data), event.id or None
                            )
                            if frame.event == "stream_error":
                                code = frame.data.get("code")
                                if (
                                    code in {"overflow", "backend_unavailable"}
                                    and not once
                                ):
                                    raise httpx.ReadError(str(code))
                                raise ValueError(f"Activity stream failed: {code}")
                            state.feed(frame)
                            if frame.event == "stream_checkpoint":
                                delay = 0.5
                                if once:
                                    console.print(state.render())
                                    return
                        raise httpx.ReadError("Activity stream disconnected")
                except ForgottenTree:
                    # Preserve the committed cursor. The server supplies retry
                    # context through its ordinary structural recovery path.
                    if once:
                        raise ValueError("Activity snapshot was incomplete") from None
                except httpx.HTTPError as exc:
                    if once:
                        raise ValueError(
                            f"Activity service unavailable: {exc}"
                        ) from exc
                state.reconnecting = True
                try:
                    await asyncio.wait_for(stop.wait(), delay)
                except TimeoutError:
                    pass
                delay = min(5, delay * 2)

    if once:
        await receive()
        return
    with closing(create_input(stdin=sys.stdin)) as terminal:

        def keys():
            if any(key.key in ("q", Keys.ControlC) for key in terminal.read_keys()):
                stop.set()

        with (
            terminal.raw_mode(),
            terminal.attach(keys),
            Live(
                state.render(), console=console, screen=True, auto_refresh=False
            ) as live,
        ):
            reader = asyncio.create_task(receive())
            stopping = asyncio.create_task(stop.wait())
            try:
                while not stop.is_set():
                    done, _ = await asyncio.wait(
                        {reader, stopping},
                        timeout=0.5,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if reader in done:
                        await reader
                        return
                    live.update(state.render(), refresh=True)
            finally:
                reader.cancel()
                stopping.cancel()
                for task in (reader, stopping):
                    with suppress(asyncio.CancelledError):
                        await task
