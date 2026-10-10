"""Atomic, fenced event storage through the package-owned backend boundary."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from .backend import ValkeyBackend
from .keys import EVENT_META, EVENT_STREAM, EVENT_AGENTS, TEAM, PRESENCE, generation_key
from . import event_scripts
from ...errors import MessagingError, EventProtocolError, EventRecoveryRequired
from ...events import (
    HubCursor,
    MAX_STREAM_BYTES,
    MAX_STREAM_EVENTS,
    encode,
)
from ...records import MANIFEST, MAX_BYTES, MAX_ENTITIES
from ...schemas import stream_id, TeamRecord


def _hash(values: Any) -> dict[str, str]:
    if isinstance(values, dict):
        return values
    return dict(zip(values[::2], values[1::2], strict=True))


class ValkeyEvents:
    def __init__(self, backend: ValkeyBackend) -> None:
        self.backend = backend

    async def _eval(
        self, script: str, keys: list[str], args: list[Any], *, agent: str | None = None
    ) -> Any:
        try:
            if agent is not None:
                return await self.backend._fenced_eval(agent, script, keys, args)
            return await self.backend._eval(script, keys, args)
        except MessagingError as exc:
            from ...errors import BackendUnavailable

            if isinstance(exc, BackendUnavailable):
                raise
            raise EventProtocolError(str(exc)) from exc

    async def _command(self, command: str, *args: Any) -> Any:
        try:
            return await self.backend._call(command, *args)
        except MessagingError as exc:
            from ...errors import BackendUnavailable

            if isinstance(exc, BackendUnavailable):
                raise
            raise EventProtocolError("Invalid event dataset") from exc

    async def initialize(self) -> dict[str, str]:
        return _hash(
            await self._eval(
                event_scripts.INIT,
                [EVENT_META, EVENT_STREAM, EVENT_AGENTS],
                [uuid4().hex],
            )
        )

    async def capture(
        self, agent: str | None = None
    ) -> tuple[dict[str, str], dict[str, Any], dict[str, str]]:
        meta, agents, participants = await self._eval(
            event_scripts.CAPTURE,
            [EVENT_META, EVENT_STREAM, EVENT_AGENTS, TEAM],
            [agent or ""],
        )
        try:
            origins = {key: json.loads(value) for key, value in _hash(agents).items()}
            for value in origins.values():
                if value.get("v") != 1:
                    raise ValueError("Origin schema version")
                stream_id(value.get("floor", "0-0"))
            public = {}
            for member, raw in _hash(participants).items():
                info = TeamRecord.decode(member, raw).model_dump(exclude={"lease"})
                public[member] = json.dumps(info)
            return _hash(meta), origins, public
        except (ValueError, TypeError, AttributeError, MessagingError) as exc:
            raise EventProtocolError("Invalid origin metadata") from exc

    async def projection(
        self, agent: str, generation: str
    ) -> dict[str, dict[str, Any]]:
        key = generation_key(agent, generation)
        result: dict[str, dict[str, Any]] = {}
        cursor = 0
        size = 0
        while True:
            cursor, page = await self._command("HSCAN", key, cursor, "COUNT", 128)
            for name, value in page.items():
                if name not in result:
                    size += len(name.encode()) + len(value.encode())
                if size > MAX_BYTES or len(result) > MAX_ENTITIES:
                    raise EventProtocolError("Oversized event generation")
                try:
                    data = json.loads(value)
                    if not isinstance(data, dict) or data.get("v") != 1:
                        raise ValueError("Unsupported event entity version")
                    if "entity" in data:
                        entity = data["entity"]
                        if not isinstance(entity, dict) or entity.get("v") != 1:
                            raise ValueError("Unsupported event entity version")
                        data = {**entity, "delivery": data["delivery"]}
                    if name == MANIFEST:
                        stream_id(data["baseline"])
                except (ValueError, TypeError, KeyError, MessagingError) as exc:
                    raise EventProtocolError("Invalid stored event entity") from exc
                result[name] = data
            if int(cursor) == 0:
                return result

    async def online_token(self, agent: str) -> str | None:
        lease = (await self.backend.lease_info(agent))["lease"]
        return lease["token"] if lease else None

    async def commit(self, op: dict[str, Any]) -> str:
        op = {
            **op,
            "old_key": generation_key(
                op["agent"], op.get("old_generation") or op["generation"]
            ),
        }
        raw = encode(op)
        keys = [
            EVENT_META,
            EVENT_STREAM,
            EVENT_AGENTS,
            TEAM,
            generation_key(op["agent"], op["generation"]),
            op.get("old_key") or generation_key(op["agent"], op["generation"]),
            PRESENCE,
        ]
        response = await self._eval(
            event_scripts.WRITE,
            keys,
            [
                "",
                MAX_STREAM_EVENTS,
                MAX_STREAM_BYTES,
                raw,
                sha256(raw.encode()).hexdigest(),
            ],
            agent=op["agent"],
        )
        return self._result(response)

    async def stage(self, op: dict[str, Any]) -> None:
        raw = encode(op)
        if len(raw.encode()) > MAX_BYTES:
            from toolang.execution.errors import SnapshotLimitError

            raise SnapshotLimitError("Snapshot upload exceeds budget")
        key = generation_key(op["agent"], op["generation"])
        response = await self._eval(
            event_scripts.STAGE,
            [EVENT_META, EVENT_STREAM, EVENT_AGENTS, TEAM, key, key, PRESENCE],
            ["", MAX_STREAM_EVENTS, MAX_STREAM_BYTES, raw],
            agent=op["agent"],
        )
        self._result(response)

    async def abandon(self, agent: str, generation: str, *, token: str) -> None:
        # Cleanup is a fenced write too. Check the lease and current generation
        # atomically so ownership changes or activation cannot race the deletion.
        response = await self._eval(
            event_scripts.ABANDON,
            [
                EVENT_META,
                EVENT_STREAM,
                EVENT_AGENTS,
                TEAM,
                generation_key(agent, generation),
                PRESENCE,
            ],
            [token, agent, generation],
            agent=agent,
        )
        self._result(response)

    @staticmethod
    def _result(response: Any) -> str:
        if response[0] == "ok":
            return response[1]
        if response[0] in {"reset", "recover", "lease"}:
            raise EventRecoveryRequired(response[0])
        raise EventProtocolError(str(response))

    async def read(
        self, cursor: HubCursor, until: str = "+"
    ) -> tuple[str, list[tuple[str, dict[str, str]]]]:
        response = await self._eval(
            event_scripts.READ,
            [EVENT_META, EVENT_STREAM, EVENT_AGENTS],
            [
                cursor.epoch,
                MAX_STREAM_EVENTS,
                MAX_STREAM_BYTES,
                cursor.stream_id,
                until,
            ],
        )
        if response[0] != "ok":
            raise EventRecoveryRequired(response[0])
        return response[1], [(sid, _hash(values)) for sid, values in response[2]]

    async def wait(self, after: str) -> None:
        await self._command(
            "XREAD", "BLOCK", 1000, "COUNT", 1, "STREAMS", EVENT_STREAM, after
        )
