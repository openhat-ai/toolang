"""Vendor-independent event encoding and Hub observation vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from uuid import UUID

from toolang.execution.events import PartBegin, PartDelta, PartEnd
from toolang.execution.observation import StreamScope
from .schemas import stream_id, target

MAX_EVENT_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_STREAM_EVENTS = 10000
PARTS = (PartBegin, PartDelta, PartEnd)


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, order=True)
class HubCursor:
    epoch: str
    position: tuple[int, int]

    def __str__(self) -> str:
        return f"h1.{self.epoch}.{self.position[0]}-{self.position[1]}"

    @property
    def stream_id(self) -> str:
        return f"{self.position[0]}-{self.position[1]}"

    @classmethod
    def parse(cls, value: str) -> HubCursor:
        try:
            version, epoch, sid = value.split(".")
            if version != "h1" or UUID(epoch).hex != epoch:
                raise ValueError
            return cls(epoch, stream_id(sid))
        except (TypeError, AttributeError, ValueError) as exc:
            raise ValueError("Invalid Hub cursor") from exc


@dataclass(frozen=True)
class HubScope:
    agent: str | None = None
    thread: str | None = None
    run: str | None = None

    def __post_init__(self) -> None:
        if self.agent is not None:
            target(self.agent, kind="agent")
        if (self.thread is not None or self.run is not None) and self.agent is None:
            raise ValueError("Thread/run scope requires an agent")
        if self.thread is not None and self.run is not None:
            raise ValueError("Select either thread or run")
        if self.thread == "" or self.run == "":
            raise ValueError("Empty scope")

    @property
    def local(self) -> StreamScope:
        return StreamScope(root=self.run, thread=self.thread)

    def data(self) -> dict[str, str]:
        if self.agent is None:
            return {"kind": "team"}
        return {**self.local.data(), "agent": self.agent}
