"""Assembled execution snapshot types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SnapshotAgent:
    """Agent section of one assembled run snapshot."""

    name: str
    root: str
    home: str


@dataclass(frozen=True, slots=True)
class SnapshotRun:
    """Run section of one assembled run snapshot."""

    run_id: str
    group: str
    origin: str
    thread_id: str
    run_strategy: str
    live_fingerprint: str
    invoke_params: dict[str, Any] = field(default_factory=dict)
    invoke_parts: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SnapshotProgram:
    """Program section of one assembled run snapshot."""

    source_path: str
    thunk: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """Stable wrapper around one prepared snapshot entry payload."""

    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SnapshotTask:
    """Task section of one assembled run snapshot."""

    provider: str
    ref: str
    name: str
    body: str
    state: str
    stage: str
    thread_id: str
    path: str


@dataclass(frozen=True, slots=True)
class SnapshotTaskServices:
    """Task-service section of one assembled run snapshot."""

    provider: str
    read: bool
    write: bool
    comment: bool
    path: str | None = None


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """Stable assembled runtime snapshot."""

    agent: SnapshotAgent
    run: SnapshotRun
    program: SnapshotProgram
    caps: tuple[SnapshotEntry, ...] = field(default_factory=tuple)
    jobs: tuple[SnapshotEntry, ...] = field(default_factory=tuple)
    tools: tuple[str, ...] = field(default_factory=tuple)
    task: SnapshotTask | None = None
    task_services: SnapshotTaskServices | None = None
