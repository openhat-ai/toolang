"""Assertions for execution event order and causal integrity."""

from __future__ import annotations

from collections.abc import Sequence

from toolang.execution.events import (
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
)
from toolang.execution.types import StepPath, Pointer, TypedPointer
from toolang.lang.types import Array


def event_labels(events: Sequence[RunEvent]) -> list[str]:
    """Render event identities for readable exact-order assertions."""

    labels: list[str] = []
    for event in events:
        if isinstance(event, RunBegin):
            labels.append(f"run_begin:{event.run}")
        elif isinstance(event, RunEnd):
            labels.append(f"run_end:{event.run}:{event.status}")
        elif isinstance(event, StepBegin):
            labels.append(f"step_begin:{event.step}:{event.kind}")
        elif isinstance(event, StepEnd):
            labels.append(f"step_end:{event.step}:{event.kind}:{event.status}")
        elif isinstance(event, PartBegin):
            labels.append(f"part_begin:{event.step}:{event.part}:{event.part_type}")
        elif isinstance(event, PartDelta):
            labels.append(f"part_delta:{event.step}:{event.part}")
        else:
            labels.append(f"part_end:{event.step}:{event.part}:{event.data.type}")
    return labels


def assert_run_event_integrity(events: Sequence[RunEvent]) -> None:
    """Assert lifecycle pairing, causal order, and terminal output agreement."""

    active_runs: set[str] = set()
    ended_runs: set[str] = set()
    active_steps: dict[StepPath, StepBegin] = {}
    ended_steps: dict[StepPath, StepEnd] = {}
    active_parts: dict[tuple[StepPath, int], PartBegin] = {}
    ended_parts: dict[tuple[StepPath, int], PartEnd] = {}

    for position, event in enumerate(events):
        where = f"event {position} ({event.type})"
        if isinstance(event, RunBegin):
            assert event.run not in active_runs, f"duplicate run begin at {where}"
            assert event.run not in ended_runs, f"run restarted at {where}"
            active_runs.add(event.run)
            continue

        if isinstance(event, StepBegin):
            run_id = event.step.run
            assert run_id in active_runs, f"step outside active run at {where}"
            assert event.step not in active_steps, f"duplicate step begin at {where}"
            assert event.step not in ended_steps, f"step restarted at {where}"
            active_steps[event.step] = event
            continue

        if isinstance(event, PartBegin):
            key = (event.step, event.part)
            assert event.step in active_steps, f"part outside active step at {where}"
            assert key not in active_parts, f"duplicate part begin at {where}"
            assert key not in ended_parts, f"part restarted at {where}"
            active_parts[key] = event
            continue

        if isinstance(event, PartDelta):
            key = (event.step, event.part)
            assert key in active_parts, f"part delta outside active part at {where}"
            continue

        if isinstance(event, PartEnd):
            key = (event.step, event.part)
            begin = active_parts.pop(key, None)
            assert begin is not None, f"part end without begin at {where}"
            assert begin.part_type == event.data.type, (
                f"part type changed before {where}"
            )
            assert key not in ended_parts, f"duplicate part end at {where}"
            ended_parts[key] = event
            continue

        if isinstance(event, StepEnd):
            begin = active_steps.pop(event.step, None)
            assert begin is not None, f"step end without begin at {where}"
            assert begin.kind == event.kind, f"step kind changed before {where}"
            assert event.step not in ended_steps, f"duplicate step end at {where}"
            if begin.started_at and event.finished_at:
                assert begin.started_at <= event.finished_at, (
                    f"step finished before it started at {where}"
                )
            open_parts = {key for key in active_parts if key[0] == event.step}
            assert not open_parts, f"terminal step has incomplete parts at {where}"
            if event.status == "succeeded":
                parts = tuple(
                    ended.data
                    for key, ended in sorted(
                        ended_parts.items(),
                        key=lambda item: (
                            item[0][0].run,
                            item[0][0].indices,
                            item[0][1],
                        ),
                    )
                    if key[0] == event.step
                )
                if parts:
                    assert event.output is not None
                    output_value = event.output.value
                    assert (
                        tuple(output_value)
                        if isinstance(output_value, Array | tuple | list)
                        else (output_value,)
                    ) == parts, f"step output differs from terminal parts at {where}"
            ended_steps[event.step] = event
            continue

        assert isinstance(event, RunEnd)
        assert event.run in active_runs, f"run end without begin at {where}"
        assert event.run not in ended_runs, f"duplicate run end at {where}"
        assert not any(step.run == event.run for step in active_steps), (
            f"run ended with active steps at {where}"
        )
        if event.output is not None and isinstance(event.output.value, TypedPointer):
            pointer = event.output.value.pointer
            assert any(Pointer.step(step) == pointer for step in ended_steps), (
                f"run output references an incomplete step at {where}"
            )
        active_runs.remove(event.run)
        ended_runs.add(event.run)

    assert not active_runs, f"runs missing terminal events: {sorted(active_runs)}"
    assert not active_steps, f"steps missing terminal events: {sorted(active_steps)}"
    assert not active_parts, f"parts missing terminal events: {sorted(active_parts)}"
