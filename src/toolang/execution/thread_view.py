"""Logical Thread histories projected from physical Runs and Thread controls."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from .records import (
    ControlRecord,
    ForkControlPayload,
    RewindControlPayload,
    RunRecord,
    ThreadRecord,
)
from .types import ControlRef, RunLink, RunRef

_Run = TypeVar("_Run", RunRecord, RunLink)


@dataclass(frozen=True)
class ThreadView:
    """One logical Thread selection, independent of physical Run ownership."""

    record: ThreadRecord
    head: ControlRef
    roots: tuple[RunRecord, ...]
    members: tuple[RunRecord, ...]
    cursor: str | None = None

    def runs(self, *, reverse: bool = False) -> tuple[RunRecord, ...]:
        return self.roots[::-1] if reverse else self.roots

    def contains(self, run: RunRef | str) -> bool:
        return any(item.id == str(run) for item in self.members)

    def tree(self) -> tuple[RunRecord, ...]:
        return self.members


class _ThreadProjection(Generic[_Run]):
    """Resolve Thread views within one immutable store snapshot."""

    def __init__(self, runs: Sequence[_Run], controls: Sequence[ControlRecord]) -> None:
        self.runs = tuple(runs)
        self._controls: dict[str, list[ControlRecord]] = {}
        self._roots: dict[str, list[_Run]] = {}
        self._root_of: dict[str, str] = {}
        self._cache: dict[tuple[str, int, bool], tuple[_Run, ...]] = {}
        for control in controls:
            if control.status == "applied":
                self._controls.setdefault(str(control.target), []).append(control)
        for run in runs:
            if run.parent is None:
                self._roots.setdefault(str(run.thread), []).append(run)
                self._root_of[run.id] = run.id
            else:
                self._root_of[run.id] = self._root_of[run.parent.run_id]

    def head(self, thread_id: str) -> ControlRef:
        """Return the latest applied control, including creation."""

        return self._controls[thread_id][-1].ref

    def prefix(self, payload: ForkControlPayload) -> tuple[_Run, ...]:
        """Resolve a fork's captured source prefix, ignoring later controls."""

        source = self.history(str(payload.fork_from), head=payload.fork_head)
        end = next(i for i, run in enumerate(source) if run.id == str(payload.fork_at))
        return source[: end + 1]

    def history(
        self,
        thread_id: str,
        *,
        head: ControlRef | None = None,
        include_rewound: bool = False,
    ) -> tuple[_Run, ...]:
        """Project roots; head limits controls, while prefix() also bounds appends."""

        controls = self._controls.get(thread_id, ())
        if not controls:
            return ()
        index = head.index if head is not None else controls[-1].index
        key = (thread_id, index, include_rewound)
        if key not in self._cache:
            creation = controls[0].payload
            prefix = (
                self.prefix(creation)
                if isinstance(creation, ForkControlPayload)
                else ()
            )
            runs = [*prefix, *self._roots.get(thread_id, ())]
            if not include_rewound:
                for control in controls:
                    payload = control.payload
                    if control.index > index:
                        break
                    if isinstance(payload, RewindControlPayload):
                        positions = {run.id: i for i, run in enumerate(runs)}
                        start = positions[str(payload.rewind_from)]
                        end = positions[str(payload.rewind_through)]
                        del runs[start : end + 1]
            self._cache[key] = tuple(runs)
        return self._cache[key]

    def tree(self, roots: Sequence[_Run]) -> tuple[_Run, ...]:
        """Return physical Runs belonging to the selected root trees."""

        selected = {run.id for run in roots}
        return tuple(run for run in self.runs if self._root_of[run.id] in selected)

    def is_forked(self, root_run_id: str) -> bool:
        """Keep every durable fork prefix frozen, including rewound prefixes."""

        return any(
            run.id == root_run_id
            for controls in self._controls.values()
            if isinstance(payload := controls[0].payload, ForkControlPayload)
            for run in self.prefix(payload)
        )
