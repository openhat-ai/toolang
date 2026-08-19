"""Terminal-independent reduction of ordered execution events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from toolang.base.types.message import TextDelta
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
from toolang.execution.types import ExecutionError, Occurrence, Pointer, StepPath
from toolang.lang.ast import FlowStmt, RepeatStmt

from .formatting import (
    count,
    elapsed,
    flow_statement,
    model_label,
    one_line,
    progress_statement_header,
    progress_until_header,
    shape_label,
    step_output_summary,
    tool_exit_code,
    tool_label,
    tool_output_summary,
    truncate,
    usage_facts,
)
from .state import Metrics

ProgressTone = Literal["progress", "active", "error", "warning"]


@dataclass(frozen=True, slots=True)
class ProgressRow:
    """One semantic progress row before surface-specific styling."""

    text: str
    tone: ProgressTone = "progress"


@dataclass(frozen=True, slots=True)
class ProgressBlock:
    """One stable or replaceable group of semantic progress rows."""

    key: str
    rows: tuple[ProgressRow, ...]


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """Append-only stable blocks plus the complete current live snapshot."""

    stable: tuple[ProgressBlock, ...] = ()
    live: tuple[ProgressBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class _LaneOwner:
    step: StepPath
    lane: int
    item: int
    run_id: str


@dataclass(slots=True)
class _Lane:
    run_id: str
    item: int
    activity: str = "· starting…"
    active: bool = True


@dataclass(slots=True)
class _Run:
    begin: RunBegin
    lane_owner: _LaneOwner | None
    metrics: Metrics = field(default_factory=lambda: Metrics(runs=1))
    end: RunEnd | None = None

    @property
    def kind(self) -> str:
        kind, separator, _name = self.begin.runnable.partition(":")
        return kind if separator else "run"


@dataclass(slots=True)
class _Step:
    begin: StepBegin
    lane_owner: _LaneOwner | None
    ordinal: int
    sequence: int
    preview: str = ""
    boundaries: tuple[str, ...] = ()
    lanes: dict[int, _Lane] = field(default_factory=dict)
    metrics: Metrics = field(default_factory=Metrics)
    child_count: int = 0
    active_children: int = 0
    succeeded_children: int = 0
    failed_children: int = 0
    canceled_children: int = 0
    iterations: int = 0
    terminating: bool = False

    @property
    def statement(self) -> FlowStmt | None:
        return flow_statement(self.begin.given)

    @property
    def is_call(self) -> bool:
        return self.statement is None


class _PresentationError(RuntimeError):
    """An event sequence cannot be represented safely."""


class ExecutionProgressReducer:
    """Reduce one ordered root Run tree into stable and live presentation blocks."""

    def __init__(self, *, show_boundaries: bool = True) -> None:
        self.show_boundaries = show_boundaries
        self._root: str | None = None
        self._root_ended = False
        self._broken = False
        self._runs: dict[str, _Run] = {}
        self._steps: dict[StepPath, _Step] = {}
        self._seen_runs: set[str] = set()
        self._seen_steps: set[StepPath] = set()
        self._outcome_shapes: dict[StepPath, str] = {}
        self._parts: set[tuple[StepPath, int]] = set()
        self._errors: dict[Pointer, ExecutionError] = {}
        self._boundary_rows: dict[str, tuple[ProgressRow, ...]] = {}
        self._boundary_claims: dict[str, str] = {}
        self._committed_boundaries: set[str] = set()
        self._repeat_ordinals: dict[tuple[StepPath, int], int] = {}
        self._sequence = 0

    @property
    def root_metrics(self) -> Metrics:
        """Return metrics accumulated for the active or completed root Run."""

        run = self._runs.get(self._root or "")
        return run.metrics if run is not None else Metrics()

    @property
    def root_kind(self) -> str:
        """Return the root runnable kind when known."""

        run = self._runs.get(self._root or "")
        return run.kind if run is not None else "run"

    def outcome_shape(self, step: StepPath) -> str:
        """Return the bounded shape retained for one terminal root Step."""

        return self._outcome_shapes.get(step, "")

    def handle(self, event: RunEvent) -> ProgressUpdate:
        """Validate and reduce one ordered event without querying durable state."""

        if self._broken:
            return ProgressUpdate()
        try:
            if self._root_ended:
                raise _PresentationError("progress event arrived after run completion")
            stable = self._reduce(event)
            return ProgressUpdate(stable=tuple(stable), live=self._live_blocks())
        except _PresentationError as exc:
            self._broken = True
            self._parts.clear()
            return ProgressUpdate(
                stable=(self._diagnostic_block(str(exc)),),
                live=(),
            )

    def diagnostic(self, message: str) -> ProgressUpdate:
        """Close live presentation with one ownerless execution diagnostic."""

        self._broken = True
        self._parts.clear()
        return ProgressUpdate(stable=(self._diagnostic_block(message),), live=())

    def _reduce(self, event: RunEvent) -> list[ProgressBlock]:
        if isinstance(event, RunBegin):
            self._begin_run(event)
            return []
        if isinstance(event, StepBegin):
            self._begin_step(event)
            return []
        if isinstance(event, PartBegin):
            self._begin_part(event)
            return []
        if isinstance(event, PartDelta):
            self._part_delta(event)
            return []
        if isinstance(event, PartEnd):
            self._end_part(event)
            return []
        if isinstance(event, StepEnd):
            block = self._end_step(event)
            return [block] if block is not None else []
        if isinstance(event, RunEnd):
            block = self._end_run(event)
            return [block] if block is not None else []
        raise _PresentationError(f"unsupported progress event: {type(event).__name__}")

    def _begin_run(self, event: RunBegin) -> None:
        if event.run in self._seen_runs:
            raise _PresentationError(f"duplicate RunBegin for {event.run}")
        self._seen_runs.add(event.run)
        lane_owner: _LaneOwner | None = None
        if event.parent is None:
            if self._root is not None:
                raise _PresentationError("multiple root Runs in one progress stream")
            self._root = event.run
        else:
            owner = self._active_step(event.parent)
            if owner.begin.kind == "par" and owner.lane_owner is None:
                lane_owner = self._direct_lane_owner(
                    event.parent,
                    event.occurrence,
                    event.run,
                )
                owner.child_count += 1
                owner.active_children += 1
                owner.lanes[lane_owner.lane] = _Lane(event.run, lane_owner.item)
            else:
                lane_owner = owner.lane_owner
        self._runs[event.run] = _Run(event, lane_owner)
        if event.parent is not None:
            self._note_iteration(event.parent, event.occurrence)
            if lane_owner is not None:
                self._set_lane_activity(lane_owner, "· starting…")

    def _end_run(self, event: RunEnd) -> ProgressBlock | None:
        run = self._runs.get(event.run)
        if run is None or run.end is not None:
            raise _PresentationError(f"RunEnd without active RunBegin for {event.run}")
        if any(step.begin.step.run == event.run for step in self._steps.values()):
            raise _PresentationError(f"RunEnd with active Step for {event.run}")
        if any(
            child.begin.parent is not None and child.begin.parent.run == event.run
            for child in self._runs.values()
        ):
            raise _PresentationError(f"RunEnd with active child Run for {event.run}")
        run.end = event
        if event.error is not None:
            self._errors[Pointer.run(event.run)] = event.error

        owner = (
            self._steps.get(run.begin.parent) if run.begin.parent is not None else None
        )
        if owner is not None:
            owner.metrics.add(run.metrics)
            for ancestor in self._flow_ancestors_in_run(owner.begin.step):
                ancestor.metrics.add(run.metrics)
            parent_run = self._runs.get(owner.begin.step.run)
            if parent_run is not None:
                parent_run.metrics.add(run.metrics)
            if owner.begin.kind == "par" and owner.lane_owner is None:
                owner.active_children -= 1
                if event.status == "succeeded":
                    owner.succeeded_children += 1
                elif event.status == "failed":
                    owner.failed_children += 1
                else:
                    owner.canceled_children += 1
                lane = self._lane_for_run(owner, event.run)
                if lane is not None:
                    lane.active = False
                if event.status == "failed":
                    self._start_canceling(owner)

        block: ProgressBlock | None = None
        if run.lane_owner is not None:
            if isinstance(event.error, str):
                self._set_lane_activity(
                    run.lane_owner,
                    f"· failed {self._error_text(event.error)}",
                )
            elif event.status == "canceled":
                self._set_lane_activity(
                    run.lane_owner,
                    "· canceled",
                )
        elif run.begin.parent is not None and isinstance(event.error, str):
            block = self._run_error_block(run, event.error)
        elif run.begin.parent is None:
            self._root_ended = True
            if isinstance(event.error, str):
                block = self._diagnostic_block(self._error_text(event.error))
            elif isinstance(event.error, Pointer) and not self._pointer_resolves(
                event.error
            ):
                block = self._diagnostic_block(
                    f"could not resolve execution error {event.error}"
                )

        if run.begin.parent is not None:
            self._runs.pop(event.run, None)
            self._outcome_shapes = {
                step: shape
                for step, shape in self._outcome_shapes.items()
                if step.run != event.run
            }
        return block

    def _begin_step(self, event: StepBegin) -> None:
        if event.step in self._seen_steps:
            raise _PresentationError(f"duplicate StepBegin for {event.step}")
        self._seen_steps.add(event.step)
        run = self._runs.get(event.step.run)
        if run is None or run.end is not None:
            raise _PresentationError(f"StepBegin without active Run {event.step.run}")
        ordinal = event.step.index
        parent = self._steps.get(event.step.parent) if event.step.parent else None
        if (
            parent is not None
            and isinstance(parent.statement, RepeatStmt)
            and event.occurrence is not None
            and event.occurrence.iteration is not None
        ):
            iteration = event.occurrence.iteration.index
            self._repeat_ordinals = {
                key: value
                for key, value in self._repeat_ordinals.items()
                if key[0] != parent.begin.step or key[1] == iteration
            }
            key = (parent.begin.step, iteration)
            ordinal = self._repeat_ordinals.get(key, 0)
            self._repeat_ordinals[key] = ordinal + 1
        self._sequence += 1
        state = _Step(event, run.lane_owner, ordinal, self._sequence)
        self._steps[event.step] = state
        self._note_iteration(event.step, event.occurrence)

        if state.lane_owner is not None:
            self._set_lane_activity(
                state.lane_owner,
                self._active_lane_activity(state),
            )
            return
        if state.is_call or event.kind == "par":
            block_key = self._block_key(state)
            state.boundaries = self._claim_boundaries(block_key, state)

    def _end_step(self, event: StepEnd) -> ProgressBlock | None:
        state = self._steps.get(event.step)
        if state is None:
            raise _PresentationError(
                f"StepEnd without active StepBegin for {event.step}"
            )
        if state.begin.kind != event.kind:
            raise _PresentationError(
                f"Step kind changed for {event.step}: "
                f"{state.begin.kind} -> {event.kind}"
            )
        if any(step == event.step for step, _part in self._parts):
            raise _PresentationError(f"StepEnd with active Part for {event.step}")
        if any(child.begin.step.parent == event.step for child in self._steps.values()):
            raise _PresentationError(f"StepEnd with active child Step for {event.step}")
        if any(child.begin.parent == event.step for child in self._runs.values()):
            raise _PresentationError(f"StepEnd with active child Run for {event.step}")
        self._outcome_shapes[event.step] = shape_label(event)
        if event.error is not None:
            self._errors[Pointer.step(event.step)] = event.error
        run = self._runs[event.step.run]
        run.metrics.record_step(event)

        block: ProgressBlock | None = None
        if state.lane_owner is not None:
            self._set_lane_activity(
                state.lane_owner,
                self._terminal_lane_activity(state, event),
            )
        elif state.is_call:
            if isinstance(event.error, Pointer):
                self._release_boundaries(state.boundaries)
            else:
                block = self._finalize_block(state, self._call_rows(state, event))
        else:
            statement = state.statement
            if statement is not None and event.kind == "par":
                block = self._finalize_block(
                    state,
                    self._par_terminal_rows(state, event),
                )
            elif isinstance(event.error, Pointer):
                self._release_boundaries(state.boundaries)
            elif (
                statement is not None
                and event.status == "succeeded"
                and statement.kind == "run"
            ):
                self._release_boundaries(state.boundaries)
            elif statement is not None:
                if not state.boundaries:
                    state.boundaries = self._claim_boundaries(
                        self._block_key(state),
                        state,
                    )
                block = self._finalize_block(state, self._flow_rows(state, event))

        self._steps.pop(event.step, None)
        self._repeat_ordinals = {
            key: value
            for key, value in self._repeat_ordinals.items()
            if key[0] != event.step
        }
        return block

    def _begin_part(self, event: PartBegin) -> None:
        self._active_step(event.step)
        key = (event.step, event.part)
        if key in self._parts:
            raise _PresentationError(
                f"duplicate PartBegin for {event.step} part {event.part}"
            )
        self._parts.add(key)

    def _part_delta(self, event: PartDelta) -> None:
        key = (event.step, event.part)
        if key not in self._parts:
            raise _PresentationError(
                f"PartDelta without active Part for {event.step} part {event.part}"
            )
        state = self._active_step(event.step)
        if isinstance(event.delta, TextDelta):
            state.preview = (state.preview + event.delta.text)[-800:]
            if state.lane_owner is not None:
                preview = truncate(one_line(state.preview), 160)
                activity = f"· thinking… {preview}" if preview else "· thinking…"
                self._set_lane_activity(
                    state.lane_owner,
                    activity,
                )

    def _end_part(self, event: PartEnd) -> None:
        key = (event.step, event.part)
        if key not in self._parts:
            raise _PresentationError(
                f"PartEnd without active Part for {event.step} part {event.part}"
            )
        self._parts.remove(key)

    def _call_rows(self, state: _Step, event: StepEnd) -> tuple[ProgressRow, ...]:
        facts = self._call_facts(state, event)
        tone = self._status_tone(event.status)
        if state.begin.kind == "model":
            if event.status == "succeeded":
                output = step_output_summary(event)
                primary = f"· executed {output}" if output else "· executed"
            elif event.status == "failed":
                error = self._error_text(event.error)
                primary = f"· failed {error}" if error else "· failed"
            else:
                error = self._error_text(event.error)
                primary = f"· canceled {error}" if error else "· canceled"
            rows = [ProgressRow(primary, tone)]
        elif state.begin.kind == "tool":
            tool = tool_label(state.begin.given)
            status = "executed" if event.status == "succeeded" else event.status
            rows = [ProgressRow(f"· {status} {tool}", tone)]
            detail = (
                tool_output_summary(event)
                if event.status == "succeeded"
                else self._error_text(event.error)
            )
            if detail:
                rows.append(ProgressRow(f"  {detail}", tone))
        else:
            status = "executed" if event.status == "succeeded" else event.status
            rows = [ProgressRow(f"· {status} {state.begin.kind}", tone)]
        if facts:
            rows.append(ProgressRow(f"  {' · '.join(facts)}"))
        return tuple(rows)

    def _flow_rows(self, state: _Step, event: StepEnd) -> tuple[ProgressRow, ...]:
        statement = state.statement
        if statement is None:
            return ()
        tone = self._status_tone(event.status)
        rows: list[ProgressRow]
        if isinstance(statement, RepeatStmt):
            if event.status == "succeeded":
                rows = [
                    ProgressRow(f"· completed · {count(state.iterations, 'iteration')}")
                ]
            else:
                rows = [ProgressRow(f"· {event.status}", tone)]
        elif event.status == "succeeded":
            output = step_output_summary(event) or shape_label(event)
            rows = [ProgressRow(f"· executed {output}" if output else "· executed")]
        else:
            rows = [ProgressRow(f"· {event.status}", tone)]
        if event.status != "succeeded" and (error := self._error_text(event.error)):
            rows.append(ProgressRow(f"  {error}", tone))
        facts = (
            self._repeat_facts(state, event)
            if isinstance(statement, RepeatStmt)
            else self._flow_facts(state, event)
        )
        if facts:
            rows.append(ProgressRow(f"  {' · '.join(facts)}"))
        return tuple(rows)

    def _par_terminal_rows(
        self,
        state: _Step,
        event: StepEnd,
    ) -> tuple[ProgressRow, ...]:
        tone = self._status_tone(event.status)
        rows = [ProgressRow(f"· {self._par_counts(state, live=False)}", tone)]
        if event.status == "succeeded":
            output = shape_label(event) or step_output_summary(event)
            if output:
                rows.append(ProgressRow(f"  {output}"))
        elif error := self._error_text(event.error):
            rows.append(ProgressRow(f"  {error}", tone))
        facts = self._flow_facts(state, event)
        if facts:
            rows.append(ProgressRow(f"  {' · '.join(facts)}"))
        return tuple(rows)

    def _run_error_block(self, run: _Run, error: str) -> ProgressBlock:
        owner = self._steps.get(run.begin.parent) if run.begin.parent else None
        boundaries = ()
        if owner is not None:
            boundaries = self._claim_boundaries(f"run:{run.begin.run}", owner)
        rows = (
            *self._rows_for_boundaries(boundaries),
            ProgressRow(f"· {self._error_text(error)}", "error"),
        )
        self._commit_boundaries(boundaries)
        return ProgressBlock(f"run:{run.begin.run}", rows)

    def _finalize_block(
        self,
        state: _Step,
        rows: tuple[ProgressRow, ...],
    ) -> ProgressBlock:
        block = ProgressBlock(
            self._block_key(state),
            (*self._rows_for_boundaries(state.boundaries), *rows),
        )
        self._commit_boundaries(state.boundaries)
        return block

    def _live_blocks(self) -> tuple[ProgressBlock, ...]:
        blocks: list[tuple[int, ProgressBlock]] = []
        for state in self._steps.values():
            if state.lane_owner is not None:
                continue
            if state.is_call:
                rows = (
                    *self._rows_for_boundaries(state.boundaries),
                    self._call_live_row(state),
                )
                blocks.append(
                    (state.sequence, ProgressBlock(self._block_key(state), rows))
                )
            elif state.begin.kind == "par":
                rows = (
                    *self._rows_for_boundaries(state.boundaries),
                    *self._par_live_rows(state),
                )
                blocks.append(
                    (state.sequence, ProgressBlock(self._block_key(state), rows))
                )
        return tuple(
            block for _sequence, block in sorted(blocks, key=lambda item: item[0])
        )

    def _call_live_row(self, state: _Step) -> ProgressRow:
        if state.begin.kind == "model":
            preview = truncate(one_line(state.preview), 180)
            text = f"· thinking… {preview}" if preview else "· thinking…"
        elif state.begin.kind == "tool":
            text = f"· executing {tool_label(state.begin.given)}…"
        else:
            text = f"· running {state.begin.kind}…"
        return ProgressRow(text, "active")

    def _par_live_rows(self, state: _Step) -> tuple[ProgressRow, ...]:
        rows = [
            ProgressRow(f"· running · {self._par_counts(state, live=True)}", "active")
        ]
        if not state.lanes:
            return tuple(rows)
        lane_width = len(str(max(state.lanes)))
        item_width = len(str(max(lane.item for lane in state.lanes.values())))
        for lane_index, lane in sorted(state.lanes.items()):
            rows.append(
                ProgressRow(
                    f"  {lane_index:>{lane_width}} | #{lane.item:>{item_width}} | "
                    f"{lane.activity}",
                    "active",
                )
            )
        return tuple(rows)

    def _par_counts(self, state: _Step, *, live: bool) -> str:
        facts = []
        if state.succeeded_children or not state.child_count:
            facts.append(f"{state.succeeded_children} succeeded")
        if state.failed_children:
            facts.append(f"{state.failed_children} failed")
        if live and state.active_children:
            status = "canceling" if state.terminating else "active"
            facts.append(f"{state.active_children} {status}")
        if state.canceled_children:
            facts.append(f"{state.canceled_children} canceled")
        return " · ".join(facts) or "0 succeeded"

    def _call_facts(self, state: _Step, event: StepEnd) -> list[str]:
        facts = [str(event.step), elapsed(state.begin.started_at, event.finished_at)]
        if state.begin.kind == "model":
            facts.extend([model_label(state.begin.given), *usage_facts(event.noted)])
        elif state.begin.kind == "tool":
            code = tool_exit_code(event)
            if code is not None:
                facts.append(f"exit {code}")
        return [fact for fact in facts if fact]

    def _flow_facts(self, state: _Step, event: StepEnd) -> list[str]:
        facts = [str(event.step)]
        facts.extend(
            state.metrics.facts(
                duration=elapsed(state.begin.started_at, event.finished_at),
                include_runs=True,
            )
        )
        return [fact for fact in facts if fact]

    def _repeat_facts(self, state: _Step, event: StepEnd) -> list[str]:
        facts = [
            f"{count(state.metrics.runs, 'run')} succeeded"
            if state.metrics.runs
            else "",
        ]
        facts.extend(
            state.metrics.facts(
                duration=elapsed(state.begin.started_at, event.finished_at),
                include_runs=False,
            )
        )
        return [fact for fact in facts if fact]

    def _claim_boundaries(self, block_key: str, target: _Step) -> tuple[str, ...]:
        if not self.show_boundaries:
            return ()
        claimed: list[str] = []
        for key, rows in self._boundary_candidates(target):
            if key in self._committed_boundaries:
                continue
            owner = self._boundary_claims.get(key)
            if owner not in {None, block_key}:
                continue
            self._boundary_claims[key] = block_key
            self._boundary_rows[key] = rows
            claimed.append(key)
        return tuple(claimed)

    def _boundary_candidates(
        self,
        target: _Step,
    ) -> list[tuple[str, tuple[ProgressRow, ...]]]:
        chain = self._flow_chain(target)
        candidates: list[tuple[str, tuple[ProgressRow, ...]]] = []
        for index, step in enumerate(chain):
            statement = step.statement
            if statement is None:
                continue
            key = f"stmt:{step.begin.step}"
            candidates.append(
                (
                    key,
                    (
                        ProgressRow(
                            f"[{step.ordinal}] {progress_statement_header(statement)}"
                        ),
                        ProgressRow(""),
                    ),
                )
            )
            if not isinstance(statement, RepeatStmt):
                continue
            occurrence = self._repeat_occurrence(chain, index, target)
            if occurrence is None or occurrence.iteration is None:
                continue
            iteration = occurrence.iteration
            if iteration.phase == "body":
                suffix = f" of {iteration.count}" if iteration.count is not None else ""
                boundary = f"--- iteration {iteration.index + 1}{suffix} ---"
                iteration_key = f"iteration:{step.begin.step}:{iteration.index}"
                candidates.append(
                    (iteration_key, (ProgressRow(boundary), ProgressRow("")))
                )
            else:
                until_key = f"until:{step.begin.step}:{iteration.index}"
                label = progress_until_header(statement)
                candidates.append(
                    (until_key, (ProgressRow(f"<?> {label}"), ProgressRow("")))
                )
        return candidates

    def _flow_chain(self, target: _Step) -> list[_Step]:
        chain: list[_Step] = []
        current: _Step | None = target if target.statement is not None else None
        if current is None:
            parent = self._parent_flow_path(target.begin.step)
            current = self._steps.get(parent) if parent is not None else None
        while current is not None:
            chain.append(current)
            parent = self._parent_flow_path(current.begin.step)
            current = self._steps.get(parent) if parent is not None else None
        chain.reverse()
        return chain

    def _flow_ancestors_in_run(self, path: StepPath) -> list[_Step]:
        ancestors: list[_Step] = []
        parent = path.parent
        while parent is not None:
            state = self._steps.get(parent)
            if state is None:
                break
            ancestors.append(state)
            parent = parent.parent
        return ancestors

    def _parent_flow_path(self, path: StepPath) -> StepPath | None:
        if path.parent is not None and path.parent in self._steps:
            return path.parent
        run = self._runs.get(path.run)
        return run.begin.parent if run is not None else None

    def _repeat_occurrence(
        self,
        chain: list[_Step],
        repeat_index: int,
        target: _Step,
    ) -> Occurrence | None:
        if repeat_index + 1 < len(chain):
            return chain[repeat_index + 1].begin.occurrence
        if target.begin.occurrence is not None:
            return target.begin.occurrence
        run = self._runs.get(target.begin.step.run)
        return run.begin.occurrence if run is not None else None

    def _rows_for_boundaries(self, keys: tuple[str, ...]) -> tuple[ProgressRow, ...]:
        rows: list[ProgressRow] = []
        for key in keys:
            if key not in self._committed_boundaries:
                rows.extend(self._boundary_rows.get(key, ()))
        return tuple(rows)

    def _commit_boundaries(self, keys: tuple[str, ...]) -> None:
        for key in keys:
            self._committed_boundaries.add(key)
            self._boundary_claims.pop(key, None)
            self._boundary_rows.pop(key, None)

    def _release_boundaries(self, keys: tuple[str, ...]) -> None:
        for key in keys:
            self._boundary_claims.pop(key, None)
            self._boundary_rows.pop(key, None)

    def _note_iteration(self, path: StepPath, occurrence: Occurrence | None) -> None:
        if occurrence is None or occurrence.iteration is None:
            return
        parent = self._steps.get(path.parent) if path.parent is not None else None
        if parent is None:
            run = self._runs.get(path.run)
            parent = (
                self._steps.get(run.begin.parent)
                if run is not None and run.begin.parent is not None
                else None
            )
        if parent is not None and isinstance(parent.statement, RepeatStmt):
            parent.iterations = max(
                parent.iterations,
                occurrence.iteration.index + 1,
            )

    def _active_lane_activity(self, state: _Step) -> str:
        if state.begin.kind == "model":
            return "· thinking…"
        if state.begin.kind == "tool":
            return f"· executing {tool_label(state.begin.given)}…"
        return "· running…"

    def _terminal_lane_activity(self, state: _Step, event: StepEnd) -> str:
        if event.status == "failed":
            error = self._error_text(event.error)
            if state.begin.kind == "tool":
                label = tool_label(state.begin.given)
                return f"· failed {label} · {error}" if error else f"· failed {label}"
            return f"· failed {error}" if error else "· failed"
        if event.status == "canceled":
            return "· canceled"
        if state.begin.kind == "model":
            output = step_output_summary(event)
            return f"· executed {output}" if output else "· executed"
        if state.begin.kind == "tool":
            label = tool_label(state.begin.given)
            output = tool_output_summary(event)
            return f"· executed {label} · {output}" if output else f"· executed {label}"
        output = step_output_summary(event)
        return f"· executed {output}" if output else "· executed"

    def _set_lane_activity(
        self,
        owner: _LaneOwner,
        activity: str,
    ) -> None:
        par = self._steps.get(owner.step)
        if par is None:
            return
        lane = par.lanes.get(owner.lane)
        if lane is not None and lane.run_id == owner.run_id:
            lane.activity = truncate(one_line(activity), 200)

    def _start_canceling(self, state: _Step, *, except_run: str | None = None) -> None:
        state.terminating = True
        for lane in state.lanes.values():
            if lane.active and lane.run_id != except_run:
                lane.activity = "· canceling…"

    @staticmethod
    def _lane_for_run(state: _Step, run_id: str) -> _Lane | None:
        return next(
            (lane for lane in state.lanes.values() if lane.run_id == run_id), None
        )

    @staticmethod
    def _direct_lane_owner(
        path: StepPath,
        occurrence: Occurrence | None,
        run_id: str,
    ) -> _LaneOwner:
        if occurrence is None or occurrence.lane is None or occurrence.item is None:
            raise _PresentationError(
                f"parallel child Run requires lane and item occurrence for {path}"
            )
        return _LaneOwner(
            path,
            occurrence.lane.index,
            occurrence.item.index,
            run_id,
        )

    def _active_step(self, path: StepPath) -> _Step:
        state = self._steps.get(path)
        if state is None:
            raise _PresentationError(f"event requires active Step {path}")
        return state

    def _pointer_resolves(self, pointer: Pointer) -> bool:
        seen: set[Pointer] = set()
        current: ExecutionError = pointer
        while isinstance(current, Pointer):
            if current in seen:
                return False
            seen.add(current)
            next_error = self._errors.get(current)
            if next_error is None:
                return False
            current = next_error
        return True

    @staticmethod
    def _error_text(error: ExecutionError | None) -> str:
        return one_line(error).strip() if isinstance(error, str) else ""

    @staticmethod
    def _status_tone(status: str) -> ProgressTone:
        if status == "failed":
            return "error"
        if status == "canceled":
            return "warning"
        return "progress"

    @staticmethod
    def _block_key(state: _Step) -> str:
        prefix = "par" if state.begin.kind == "par" else "step"
        return f"{prefix}:{state.begin.step}"

    @staticmethod
    def _diagnostic_block(message: str) -> ProgressBlock:
        return ProgressBlock(
            "run:diagnostic",
            (ProgressRow(f"· {one_line(message)}", "error"),),
        )
