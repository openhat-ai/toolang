"""Contracts for Toolang memory plugins."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass(frozen=True, slots=True)
class MemoryLimits:
    """Recall limits passed to one memory plugin."""

    max_items: int = 20
    max_chars: int = 8_000


@dataclass(frozen=True, slots=True)
class MemoryItem:
    """One recalled memory item."""

    text: str
    score: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryFact:
    """One structured recalled fact."""

    text: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemorySummary:
    """One recalled summary."""

    text: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """One memory entry to persist after a run."""

    text: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryRecallRequest:
    """One runtime recall request."""

    agent_uri: str
    agent_id: str
    thread_id: str
    run_id: str
    origin: str
    sender: str
    query_text: str | None
    execution_strategy: str
    limits: MemoryLimits = field(default_factory=MemoryLimits)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryRecallResult:
    """One completed memory recall result."""

    items: list[MemoryItem] = field(default_factory=list)
    facts: list[MemoryFact] = field(default_factory=list)
    summaries: list[MemorySummary] = field(default_factory=list)
    provider: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryWriteBatch:
    """One runtime memory write batch."""

    agent_uri: str
    agent_id: str
    thread_id: str
    run_id: str
    origin: str
    entries: list[MemoryEntry] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryWriteResult:
    """One completed memory write result."""

    written: int = 0
    provider: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


class MemoryPlugin(Protocol):
    """Protocol implemented by one loaded memory plugin instance."""

    def recall(self, request: MemoryRecallRequest) -> MemoryRecallResult:
        """Recall memory for one run."""

    def remember(self, batch: MemoryWriteBatch) -> MemoryWriteResult:
        """Persist memory after one run."""

    def health(self) -> dict[str, Any]:
        """Return one health snapshot."""


MemoryPluginFactory = Callable[[dict[str, Any]], MemoryPlugin]
