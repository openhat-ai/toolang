"""Consistent Hub attachments and independent bounded backend readers."""

from __future__ import annotations

import asyncio
from collections import deque
import time
import json
from typing import Any

from toolang.execution.errors import (
    StreamGapError,
    StreamOverflowError,
    SnapshotLimitError,
)
from toolang.execution.events import RunRetried, event_from_data
from toolang.execution.observation import (
    SnapshotBudget,
    SnapshotFrames,
    StreamNormalizer,
)
from toolang.execution.schemas import StreamFrame
from toolang.execution.stream import CanonicalEvent
from toolang.execution.types import EventCursor
from .errors import (
    EventProtocolError,
    EventRecoveryRequired,
    MessagingError,
    ScopeUnavailable,
)
from .event_backend import EventBackend, MANIFEST
from .events import (
    HubCursor,
    HubScope,
    Projection,
)
from .schemas import stream_id


def _canonical(data: str) -> CanonicalEvent:
    try:
        row = json.loads(data)
        if row.get("v") != 1:
            raise ValueError("Unsupported event version")
        return CanonicalEvent(
            EventCursor.parse(row["source_cursor"]),
            event_from_data(row["event"]),
            row["thread"],
            row["root"],
            len(data.encode()),
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise EventProtocolError("Invalid canonical event record") from exc


def envelope(
    agent: str, frame: StreamFrame, cursor: str, *, context: bool = False
) -> StreamFrame:
    data = {
        **frame.data,
        "agent": agent,
        "source_cursor": frame.data.get("cursor"),
        "cursor": cursor,
    }
    if context:
        data["context"] = True
    return StreamFrame(frame.event, data, None if context else cursor)


class HubSubscription:
    def __init__(
        self, backend: EventBackend, scope: HubScope, after: str | None = None
    ) -> None:
        self.backend, self.scope = backend, scope
        self.after = HubCursor.parse(after) if after is not None else None
        self.cursor = self.after
        self._prefix: deque[StreamFrame] = deque()
        self._batch: deque[tuple[str, dict[str, str]]] = deque()
        self._normalizers: dict[str, StreamNormalizer] = {}
        self._status: dict[str, dict[str, Any]] = {}
        self._closing_tail: str | None = None
        self._closed = False
        self._last_presence = 0.0

    async def prepare(self) -> None:
        try:
            async with asyncio.timeout(5):
                await self._prepare()
        except TimeoutError as exc:
            raise SnapshotLimitError("Hub snapshot expired") from exc

    async def _prepare(self) -> None:
        deadline = time.monotonic() + 5
        await self.backend.initialize()
        while True:
            if time.monotonic() > deadline:
                raise SnapshotLimitError("Hub snapshot expired")
            budget = SnapshotBudget(deadline)
            meta, origins, directory = await self.backend.capture(self.scope.agent)
            budget.take(dict(meta=meta, origins=origins, directory=directory))
            known = {key for key in directory if key.startswith("agent:")} | set(
                origins
            )
            if self.scope.agent is not None:
                if self.scope.agent not in known:
                    raise ScopeUnavailable("Unknown agent")
                known = {self.scope.agent}
            boundary = HubCursor(meta["epoch"], stream_id(meta["tail"]))
            same = self.after is not None and self.after.epoch == boundary.epoch
            if (
                same
                and self.after is not None
                and self.after.position > boundary.position
            ):
                raise ValueError("Hub cursor is ahead of the stream")
            cached = (
                same
                and self.after is not None
                and self.after.position >= stream_id(meta["floor"])
            )
            projections: dict[str, Projection] = {}
            baselines: dict[str, str] = {}
            status: dict[str, dict[str, Any]] = {}
            resets: set[str] = set()
            count = 0
            for agent in sorted(known):
                origin = origins.get(agent, {})
                entities = (
                    await self.backend.projection(agent, origin["generation"])
                    if origin.get("generation")
                    else {}
                )
                manifest = entities.pop(MANIFEST, None)
                if origin.get("generation") and (
                    manifest is None or manifest.get("count") != len(entities)
                ):
                    # A concurrent structural commit can change both count and
                    # generation. Stable missing/corrupt data requires repair.
                    current_meta, current_origins, _ = await self.backend.capture(agent)
                    current = current_origins.get(agent, {})
                    if (
                        current_meta["epoch"] == meta["epoch"]
                        and current.get("generation") == origin.get("generation")
                        and current.get("revision") == origin.get("revision")
                    ):
                        raise EventProtocolError("Incomplete event generation")
                    break
                budget.take(entities)
                count += len(entities)
                if count > 10000:
                    raise SnapshotLimitError("Hub snapshot entity budget exceeded")
                projections[agent] = Projection(entities)
                baselines[agent] = manifest["baseline"] if manifest else "0-0"
                token = await self.backend.online_token(agent)
                complete = origin.get("status") == "complete" and (
                    token is None or token == origin.get("token")
                )
                status[agent] = dict(
                    agent=agent,
                    online=token is not None,
                    complete=complete,
                    reason=None if complete else origin.get("reason") or "unpublished",
                )
                if self.after is not None and (
                    not same
                    or self.after.position < stream_id(origin.get("floor", "0-0"))
                ):
                    resets.add(agent)
            else:
                rows: list[tuple[str, dict[str, str]]] = []
                if cached and not resets and self.after is not None:
                    position = self.after
                    try:
                        while position.position < boundary.position:
                            _, batch = await self.backend.read(
                                position, boundary.stream_id
                            )
                            if not batch:
                                break
                            for sid, row in batch:
                                budget.take(row)
                                if row["agent"] in known:
                                    rows.append((sid, row))
                            position = HubCursor(
                                boundary.epoch, stream_id(batch[-1][0])
                            )
                    except EventRecoveryRequired:
                        continue
                check, check_origins, check_directory = await self.backend.capture(
                    self.scope.agent
                )
                if check["epoch"] != meta["epoch"] or (
                    self.scope.agent is None
                    and (
                        check["catalog_revision"] != meta["catalog_revision"]
                        or {key for key in check_directory if key.startswith("agent:")}
                        != {key for key in directory if key.startswith("agent:")}
                    )
                ):
                    continue
                if any(
                    (
                        check_origins.get(agent, {}).get("revision"),
                        check_origins.get(agent, {}).get("generation"),
                        check_origins.get(agent, {}).get("token"),
                    )
                    != (
                        origins.get(agent, {}).get("revision"),
                        origins.get(agent, {}).get("generation"),
                        origins.get(agent, {}).get("token"),
                    )
                    for agent in known
                ):
                    continue
                if (
                    cached
                    and self.after is not None
                    and stream_id(check["floor"]) > self.after.position
                ):
                    continue
                try:
                    self._build(
                        projections,
                        baselines,
                        status,
                        rows,
                        boundary,
                        cached,
                        resets,
                        deadline,
                    )
                except ScopeUnavailable:
                    raise
                except (
                    ValueError,
                    KeyError,
                    TypeError,
                    AttributeError,
                    StreamGapError,
                    MessagingError,
                ) as exc:
                    raise EventProtocolError("Invalid structural projection") from exc
                self._status = status
                self.cursor = boundary
                self._last_presence = time.monotonic()
                return
            await asyncio.sleep(0)

    def _build(
        self, projections, baselines, statuses, rows, boundary, cached, resets, deadline
    ) -> None:
        scope = self.scope.local
        if (
            self.scope.run is not None
            and any(status["complete"] for status in statuses.values())
            and not any(
                value.get("root") == self.scope.run
                for p in projections.values()
                for value in p.entities.values()
            )
        ):
            raise ScopeUnavailable("Run is not retained")
        if self.scope.thread is not None and not any(
            value.get("thread") == self.scope.thread
            or value.get("event", {}).get("source_thread") == self.scope.thread
            for p in projections.values()
            for value in p.entities.values()
        ):
            # Registered-but-unpublished agents remain valid incomplete scopes.
            if any(status["complete"] for status in statuses.values()):
                raise ScopeUnavailable("Thread is not retained")
        grouped: dict[str, list[CanonicalEvent]] = {agent: [] for agent in projections}
        deliveries = {}
        replacement = not cached or bool(resets)
        for sid, row in rows:
            if row["kind"] != "event":
                replacement = True
                resets.add(row["agent"])
                continue
            frame = _canonical(row["data"])
            if scope.matches(frame):
                grouped[row["agent"]].append(frame)
                deliveries[(row["agent"], str(frame.cursor))] = sid
                if isinstance(frame.event, RunRetried):
                    replacement = True
        prefix = SnapshotFrames(deadline)
        normalizers = {}
        ordered = []
        snapshots = {}
        selected_roots = {}
        for agent, projection in projections.items():
            roots = {
                value["root"]
                for key, value in projection.entities.items()
                if key.startswith('["root",')
                and (scope.root is None or value["root"] == scope.root)
                and (scope.thread is None or value["thread"] == scope.thread)
                and (
                    scope.root is not None
                    or agent in resets
                    or not value["terminal"]
                    or self.after is not None
                    and self.after.epoch == boundary.epoch
                    and stream_id(value.get("delivery") or baselines[agent])
                    > self.after.position
                )
            }
            roots.update(
                frame.root_run_id
                for frame in grouped[agent]
                if frame.root_run_id is not None
            )
            selected_roots[agent] = roots
            snapshot = projection.snapshot(scope, roots)
            snapshots[agent] = snapshot
            normal = StreamNormalizer()
            normal.seed(snapshot)
            normalizers[agent] = normal
            if not replacement:
                try:
                    replay = normal.replay(
                        snapshot, tuple(grouped[agent]), deadline=deadline
                    )
                    pending = []
                    for item in replay:
                        if item.id is None:
                            pending.append(
                                envelope(agent, item, str(boundary), context=True)
                            )
                        else:
                            sid = deliveries[(agent, item.id)]
                            ordered.append(
                                (
                                    stream_id(sid),
                                    [
                                        *pending,
                                        envelope(
                                            agent,
                                            item,
                                            str(
                                                HubCursor(
                                                    boundary.epoch, stream_id(sid)
                                                )
                                            ),
                                        ),
                                    ],
                                )
                            )
                            pending = []
                    if pending:
                        ordered.append((boundary.position, pending))
                except (StreamGapError, KeyError):
                    replacement = True
        if replacement:
            # Recompute every origin's suppression after a failed replay attempt.
            entire = self.after is not None and self.after.epoch != boundary.epoch
            manifest = (
                None
                if entire
                else [
                    {
                        "agent": agent,
                        "roots": None
                        if agent in resets
                        else sorted(selected_roots[agent]),
                    }
                    for agent in projections
                ]
            )
            prefix.append(
                StreamFrame(
                    "stream_prefill",
                    dict(
                        cursor=str(boundary), scope=self.scope.data(), replace=manifest
                    ),
                )
            )
            for agent, projection in projections.items():
                prefix.append(StreamFrame("stream_status", statuses[agent]))
                normalizers[agent].seed(snapshots[agent])
                for value in projection.controls(scope):
                    prefix.append(
                        envelope(
                            agent,
                            StreamFrame.source(
                                event_from_data(value["event"]),
                                value["source"],
                                context=True,
                            ),
                            str(boundary),
                            context=True,
                        )
                    )
                for item in snapshots[agent].structural():
                    prefix.append(envelope(agent, item, str(boundary), context=True))
        else:
            for status in statuses.values():
                prefix.append(StreamFrame("stream_status", status))
            for _, items in sorted(ordered, key=lambda item: item[0]):
                prefix.extend(items)
        if sum(normal.open_entities for normal in normalizers.values()) > 4096:
            raise StreamOverflowError("Hub open-entity budget exceeded")
        prefix.append(
            StreamFrame("stream_checkpoint", {"cursor": str(boundary)}, str(boundary))
        )
        self._prefix = deque(prefix)
        self._normalizers = normalizers
        self._batch.clear()
        self._closing_tail = None

    def _terminal(self) -> bool:
        return (
            self.scope.run is not None
            and all(status["complete"] for status in self._status.values())
            and not any(normal.active for normal in self._normalizers.values())
        )

    async def _presence(self) -> None:
        self._last_presence = time.monotonic()
        _, origins, directory = await self.backend.capture(self.scope.agent)
        agents = {a for a in directory if a.startswith("agent:")} | set(origins)
        if self.scope.agent is not None:
            agents &= {self.scope.agent}
        if agents != set(self._normalizers):
            await self.prepare()
            return
        for agent, status in tuple(self._status.items()):
            token = await self.backend.online_token(agent)
            origin = origins.get(agent, {})
            complete = origin.get("status") == "complete" and (
                token is None or token == origin.get("token")
            )
            current = dict(
                agent=agent,
                online=token is not None,
                complete=complete,
                reason=None if complete else origin.get("reason") or "unpublished",
            )
            if current != status:
                self._status[agent] = current
                self._prefix.append(StreamFrame("stream_status", current))
        self._prefix.append(
            StreamFrame(
                "stream_checkpoint",
                {"cursor": str(self.cursor)},
                str(self.cursor),
            )
        )

    async def receive(self) -> StreamFrame:
        while not self._closed:
            if self._prefix:
                frame = self._prefix.popleft()
                if frame.id is not None:
                    self.after = HubCursor.parse(frame.id)
                return frame
            assert self.cursor is not None
            if not self._batch:
                await asyncio.sleep(0)
                try:
                    tail, rows = await self.backend.read(
                        self.cursor, self._closing_tail or "+"
                    )
                except EventRecoveryRequired as exc:
                    raise StreamOverflowError(str(exc)) from exc
                if time.monotonic() - self._last_presence >= 15:
                    await self._presence()
                    if self._prefix:
                        continue
                if self._terminal() and self._closing_tail is None:
                    self._closing_tail = tail
                self._batch.extend(rows)
                if not rows:
                    if self._terminal() and self._closing_tail is not None:
                        raise StopAsyncIteration
                    self._closing_tail = None
                    await self.backend.wait(self.cursor.stream_id)
                    continue
            sid, row = self._batch.popleft()
            position = HubCursor(self.cursor.epoch, stream_id(sid))
            if row["agent"] not in self._normalizers:
                if self.scope.agent is None:
                    await self.prepare()
                    continue
            elif row["kind"] != "event":
                # Reattach at the last emitted position, discarding the rest of
                # this acquired batch. The new boundary covers its suffix.
                await self.prepare()
                continue
            else:
                frame = _canonical(row["data"])
                if self.scope.local.matches(frame) and self._normalizers[
                    row["agent"]
                ].advance(frame.event):
                    if (
                        sum(
                            normal.open_entities
                            for normal in self._normalizers.values()
                        )
                        > 4096
                    ):
                        raise StreamOverflowError("Hub open-entity budget exceeded")
                    self.cursor = position
                    self.after = position
                    if not self._terminal():
                        self._closing_tail = None
                    return envelope(
                        row["agent"],
                        StreamFrame.source(frame.event, str(frame.cursor)),
                        str(position),
                    )
            self.cursor = position
            if not self._batch:
                # Checkpoint filtered/suppressed traffic once per bounded batch.
                self.after = position
                return StreamFrame(
                    "stream_checkpoint", {"cursor": str(position)}, str(position)
                )
        raise StopAsyncIteration

    def close(self) -> None:
        self._closed = True
        self._prefix.clear()
        self._batch.clear()
