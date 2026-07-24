"""Execution events and caller-facing observation contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from toolang.base.types.message import Delta, Part, PartType

from .records import (
    OutputRef,
    RunControlRef,
    StepInputItem,
    ThreadControlRef,
    ThreadPeer,
)
from .types import RunStatus, StepKind, StepPath, StepStatus


@dataclass(frozen=True, slots=True)
class RunBegin:
    """A run started executing."""

    run: str
    input: RunControlRef
    context: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    type: str = "run_begin"


@dataclass(frozen=True, slots=True)
class StepBegin:
    """A run step started executing."""

    step: StepPath
    kind: StepKind
    input: tuple[StepInputItem, ...] = ()
    given: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    type: str = "step_begin"


@dataclass(frozen=True, slots=True)
class PartBegin:
    """A streamed step part started."""

    step: StepPath
    part: int
    type_: PartType
    type: str = "part_begin"


@dataclass(frozen=True, slots=True)
class PartDelta:
    """A streamed step part produced a delta."""

    step: StepPath
    part: int
    delta: Delta
    type: str = "part_delta"


@dataclass(frozen=True, slots=True)
class PartEnd:
    """A streamed step part completed."""

    step: StepPath
    part: int
    data: Part
    type: str = "part_end"


@dataclass(frozen=True, slots=True)
class StepEnd:
    """A run step reached a terminal state."""

    step: StepPath
    kind: StepKind
    status: StepStatus
    output: tuple[Part, ...] = ()
    noted: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    finished_at: str = ""
    type: str = "step_end"


@dataclass(frozen=True, slots=True)
class RunEnd:
    """A run reached a terminal state."""

    run: str
    status: RunStatus
    input: RunControlRef | None = None
    output: OutputRef | None = None
    error: str | None = None
    finished_at: str = ""
    type: str = "run_end"


RunEvent = RunBegin | StepBegin | PartBegin | PartDelta | PartEnd | StepEnd | RunEnd


class RunTracer(ABC):
    """Observe the ordered run tree started by one caller."""

    @abstractmethod
    async def on_event(self, event: RunEvent) -> None:
        """Observe one run event after durable projection."""


@dataclass(frozen=True, slots=True)
class ThreadCreated:
    """A thread was created successfully."""

    thread: str
    control: ThreadControlRef
    origin: str
    peer: ThreadPeer
    created_at: str
    type: str = "thread_created"


@dataclass(frozen=True, slots=True)
class ThreadForked:
    """A thread was forked successfully."""

    thread: str
    control: ThreadControlRef
    source_thread: str
    anchor_run: str
    created_at: str
    type: str = "thread_forked"


@dataclass(frozen=True, slots=True)
class ThreadRewound:
    """A thread was rewound successfully."""

    thread: str
    control: ThreadControlRef
    anchor_run: str
    superseded_runs: tuple[str, ...]
    created_at: str
    type: str = "thread_rewound"


ThreadEvent = ThreadCreated | ThreadForked | ThreadRewound


class ThreadListener(ABC):
    """Observe committed thread lifecycle events."""

    @abstractmethod
    def on_event(self, event: ThreadEvent) -> None:
        """Observe one successful thread mutation."""
