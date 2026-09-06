"""Pure interpretation of a selected Run's durable execution facts."""

from __future__ import annotations

from dataclasses import dataclass

from .records import ControlRecord, RunRecord, StepRecord
from .types import StepBoundary


@dataclass(frozen=True)
class RunView:
    """A record page and the controls needed to interpret its Step boundaries.

    Entries are paginated once. Dependencies can recur on adjacent pages and
    are not additional timeline messages or members of the paginated sequence.
    """

    record: RunRecord
    entries: tuple[StepRecord | ControlRecord, ...]
    dependencies: tuple[ControlRecord, ...] = ()
    cursor: str | None = None

    def steps(self, *, reverse: bool = False) -> tuple[StepRecord, ...]:
        steps = tuple(
            sorted(
                (item for item in self.entries if isinstance(item, StepRecord)),
                key=lambda item: item.ref.indices,
            )
        )
        return steps[::-1] if reverse else steps

    def controls(self) -> tuple[ControlRecord, ...]:
        """Return owned raw controls, including those never consumed by a Step."""

        return tuple(item for item in self.entries if isinstance(item, ControlRecord))

    def timeline(self) -> tuple[StepBoundary, ...]:
        """Expose adoption and interruption, without interpreting message policy."""

        events: list[tuple[tuple[int, ...], StepBoundary]] = []
        steps = self.steps()
        after_children = max((max(step.ref.indices) for step in steps), default=0) + 1
        for step in steps:
            events.append(
                (
                    (*step.ref.indices, -1),
                    StepBoundary(
                        step.ref,
                        "begin",
                        step.preceded_by,
                    ),
                )
            )
            if step.status != "running":
                events.append(
                    (
                        (*step.ref.indices, after_children),
                        StepBoundary(
                            step.ref,
                            "end",
                            (step.aborted_by,) if step.aborted_by else (),
                        ),
                    )
                )
        return tuple(event for _, event in sorted(events, key=lambda item: item[0]))
