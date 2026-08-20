"""Terminal-independent reduction of ordered execution events."""

from __future__ import annotations

from toolang.base.types.message import TextDelta, TextPart
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
from toolang.execution.types import (
    CollectionStepNoted,
    ExecutionError,
    Occurrence,
    Pointer,
    RunStatus,
    StepPath,
)
from toolang.lang.ast import (
    DropStmt,
    KeepStmt,
    MapStmt,
    RankStmt,
    RepeatStmt,
    SettleStmt,
    StormStmt,
)

from .formatting import elapsed, one_line, output_parts
from .headers import statement_header, until_header
from .state import (
    LaneOwner,
    LaneState,
    Metrics,
    RunState,
    StepState,
    step_detail,
)
from .step_projection import (
    collection_terminal_rows,
    flow_lane_terminal_lines,
    flow_error_rows,
    flow_terminal_rows,
    lane_live_text,
    lane_run_error_lines,
    lane_terminal_lines,
    trace_live_rows,
    loop_terminal_rows,
    trace_terminal_rows,
)
from .streaming_markdown import split_stable_markdown
from .types import ProgressBlock, ProgressRow, ProgressUpdate


class _PresentationError(RuntimeError):
    """An event sequence cannot be represented safely."""


class ProgressProjector:
    """Project one ordered root Run tree into committed and live progress blocks."""

    def __init__(self, *, show_boundaries: bool = True) -> None:
        self.show_boundaries = show_boundaries
        self._root: str | None = None
        self._root_ended = False
        self._broken = False
        self._runs: dict[str, RunState] = {}
        self._steps: dict[StepPath, StepState] = {}
        self._seen_runs: set[str] = set()
        self._seen_steps: set[StepPath] = set()
        self._parts: dict[tuple[StepPath, int], str] = {}
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

    def handle(self, event: RunEvent) -> ProgressUpdate:
        """Validate and reduce one ordered event without querying durable state."""

        if self._broken:
            return ProgressUpdate()
        try:
            if self._root_ended:
                raise _PresentationError("progress event arrived after run completion")
            committed = self._reduce(event)
            return ProgressUpdate(
                committed=tuple(committed),
                live=self._live_blocks(),
            )
        except _PresentationError as exc:
            self._broken = True
            self._parts.clear()
            return ProgressUpdate(
                committed=(self._diagnostic_block(str(exc)),),
                live=(),
            )

    def diagnostic(self, message: str) -> ProgressUpdate:
        """Close live presentation with one ownerless execution diagnostic."""

        self._broken = True
        self._parts.clear()
        return ProgressUpdate(committed=(self._diagnostic_block(message),), live=())

    def _reduce(self, event: RunEvent) -> list[ProgressBlock]:
        if isinstance(event, RunBegin):
            self._begin_run(event)
            return []
        if isinstance(event, StepBegin):
            block = self._begin_step(event)
            return [block] if block is not None else []
        if isinstance(event, PartBegin):
            self._begin_part(event)
            return []
        if isinstance(event, PartDelta):
            block = self._part_delta(event)
            return [block] if block is not None else []
        if isinstance(event, PartEnd):
            block = self._end_part(event)
            return [block] if block is not None else []
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
        lane_owner: LaneOwner | None = None
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
                active_lane = owner.par.lanes.get(lane_owner.lane)
                if active_lane is not None and active_lane.active:
                    raise _PresentationError(
                        f"parallel lane {lane_owner.lane} already owns active Run "
                        f"{active_lane.run_id}"
                    )
                owner.par.child_count += 1
                owner.par.active_children += 1
                if event.occurrence is not None and event.occurrence.item is not None:
                    total_items = event.occurrence.item.count
                    if owner.par.total_items not in {None, total_items}:
                        raise _PresentationError(
                            f"parallel item total changed for {event.parent}"
                        )
                    owner.par.total_items = total_items
                owner.par.lanes[lane_owner.lane] = LaneState(
                    event.run,
                    lane_owner.item,
                )
            else:
                lane_owner = owner.lane_owner
        self._runs[event.run] = RunState(event, lane_owner)
        if event.parent is not None:
            self._note_iteration(event.parent, event.occurrence)
            if lane_owner is not None:
                self._set_lane_activity(lane_owner, "• starting")

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
                owner.par.active_children -= 1
                if event.status == "succeeded":
                    owner.par.succeeded_children += 1
                elif event.status == "failed":
                    owner.par.failed_children += 1
                else:
                    owner.par.canceled_children += 1
                lane = self._lane_for_run(owner, event.run)
                if lane is not None:
                    lane.active = False
                    lane.status = event.status
                if event.status == "failed":
                    self._start_canceling(owner)

        block: ProgressBlock | None = None
        if run.lane_owner is not None:
            if isinstance(event.error, str):
                self._set_lane_terminal(
                    run.lane_owner,
                    lane_run_error_lines(self._error_text(event.error)),
                    status=event.status,
                )
            elif event.status == "canceled":
                self._set_lane_terminal(
                    run.lane_owner,
                    ("• canceled",),
                    status=event.status,
                )
        elif run.begin.parent is not None and isinstance(event.error, str):
            if not (
                event.status == "canceled"
                and run.cancellation_reported
                and self._is_generic_cancellation(event.error)
            ):
                block = self._run_error_block(run, event.error)
                if event.status == "canceled":
                    self._mark_cancellation_reported(run.begin.parent)
        elif run.begin.parent is None:
            self._root_ended = True
            if isinstance(event.error, str):
                if not (
                    event.status == "canceled"
                    and run.cancellation_reported
                    and self._is_generic_cancellation(event.error)
                ):
                    block = self._diagnostic_block(self._error_text(event.error))
            elif isinstance(event.error, Pointer) and not self._pointer_resolves(
                event.error
            ):
                block = self._diagnostic_block(
                    f"could not resolve execution error {event.error}"
                )

        if run.begin.parent is not None:
            self._runs.pop(event.run, None)
        return block

    def _begin_step(self, event: StepBegin) -> ProgressBlock | None:
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
        state = StepState(
            event,
            run.lane_owner,
            ordinal,
            self._sequence,
            step_detail(event.kind),
        )
        self._steps[event.step] = state
        self._note_iteration(event.step, event.occurrence)

        if state.lane_owner is not None:
            self._set_lane_activity(
                state.lane_owner,
                lane_live_text(
                    state.begin,
                    state.model.lane_preview if event.kind == "model" else "",
                ),
            )
            return None
        block_key = self._block_key(state)
        state.boundaries = self._claim_boundaries(block_key, state)
        rows = self._rows_for_boundaries(state.boundaries)
        if not rows:
            return None
        self._commit_boundaries(state.boundaries)
        state.boundaries = ()
        return ProgressBlock(block_key, rows)

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
        if event.error is not None:
            self._errors[Pointer.step(event.step)] = event.error
        if (
            event.kind == "model"
            and event.status == "succeeded"
            and state.model.completed_parts
        ):
            completed = tuple(
                part for _index, part in sorted(state.model.completed_parts.items())
            )
            if completed != output_parts(event):
                raise _PresentationError(
                    f"StepEnd output does not match completed Parts for {event.step}"
                )
        run = self._runs[event.step.run]
        run.metrics.record_step(event)
        if isinstance(event.noted, CollectionStepNoted) and event.kind == "par":
            if state.par.total_items not in {None, event.noted.total_items}:
                raise _PresentationError(
                    f"parallel item total changed for {event.step}"
                )
            state.par.total_items = event.noted.total_items

        block: ProgressBlock | None = None
        if state.lane_owner is not None:
            if not isinstance(event.error, Pointer):
                statement = state.statement
                if state.is_flow:
                    assert statement is not None
                    terminal = flow_lane_terminal_lines(
                        event,
                        statement=statement,
                        error=self._error_text(event.error),
                        observed_iterations=(
                            state.loop.iterations if event.kind == "loop" else 0
                        ),
                    )
                else:
                    terminal = lane_terminal_lines(
                        state.begin,
                        event,
                        error=self._error_text(event.error),
                    )
                if terminal:
                    lane = self._lane_state(state.lane_owner)
                    if not (
                        event.kind == "par"
                        and event.status == "failed"
                        and lane is not None
                        and lane.terminal_status == "failed"
                    ):
                        self._set_lane_terminal(
                            state.lane_owner,
                            terminal,
                            status=event.status,
                        )
        elif not state.is_flow:
            if isinstance(event.error, Pointer):
                self._release_boundaries(state.boundaries)
            else:
                rows = trace_terminal_rows(
                    state.begin,
                    event,
                    error=self._error_text(event.error),
                    include_model_text=not (
                        event.kind == "model" and state.model.completed_parts
                    ),
                )
                block = self._commit_block(state, rows) if rows else None
        else:
            statement = state.statement
            if statement is not None and event.kind == "par":
                block = self._commit_block(
                    state,
                    self._par_terminal_rows(state, event),
                )
            elif statement is not None:
                rows = self._flow_terminal_rows(state, event)
                if rows and not state.boundaries:
                    state.boundaries = self._claim_boundaries(
                        self._block_key(state),
                        state,
                    )
                if rows:
                    block = self._commit_block(state, rows)
                else:
                    self._release_boundaries(state.boundaries)

        if (
            event.status == "canceled"
            and block is not None
            and not state.cancellation_reported
        ):
            self._mark_cancellation_reported(event.step)

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
        self._parts[key] = event.part_type

    def _part_delta(self, event: PartDelta) -> ProgressBlock | None:
        key = (event.step, event.part)
        if key not in self._parts:
            raise _PresentationError(
                f"PartDelta without active Part for {event.step} part {event.part}"
            )
        state = self._active_step(event.step)
        if isinstance(event.delta, TextDelta) and state.begin.kind == "model":
            if self._parts[key] != "text":
                raise _PresentationError(
                    f"TextDelta changed Part type for {event.step} part {event.part}"
                )
            if state.model.text_part not in {None, event.part}:
                raise _PresentationError(
                    f"Model Step has multiple streamed Text Parts for {event.step}"
                )
            state.model.text_part = event.part
            state.model.streamed += event.delta.text
            state.model.pending += event.delta.text
            if state.lane_owner is not None:
                self._set_lane_activity(
                    state.lane_owner,
                    lane_live_text(state.begin, state.model.lane_preview),
                )
                return None
            committed, state.model.pending = split_stable_markdown(state.model.pending)
            return self._commit_model_markdown(state, committed)
        return None

    def _end_part(self, event: PartEnd) -> ProgressBlock | None:
        key = (event.step, event.part)
        if key not in self._parts:
            raise _PresentationError(
                f"PartEnd without active Part for {event.step} part {event.part}"
            )
        part_type = self._parts.pop(key)
        state = self._active_step(event.step)
        if state.begin.kind != "model":
            return None
        state.model.completed_parts[event.part] = event.data
        if not isinstance(event.data, TextPart):
            return None
        if part_type != "text":
            raise _PresentationError(
                f"PartEnd changed Part type for {event.step} part {event.part}"
            )
        if state.model.text_part not in {None, event.part}:
            raise _PresentationError(
                f"Model Step has multiple streamed Text Parts for {event.step}"
            )
        state.model.text_part = event.part
        if not event.data.text.startswith(state.model.streamed):
            raise _PresentationError(
                f"PartEnd text does not extend TextDelta for {event.step} part "
                f"{event.part}"
            )
        state.model.pending += event.data.text[len(state.model.streamed) :]
        state.model.streamed = event.data.text
        if state.lane_owner is not None:
            return None
        pending = state.model.pending
        state.model.pending = ""
        return self._commit_model_markdown(state, pending)

    def _commit_model_markdown(
        self,
        state: StepState,
        source: str,
    ) -> ProgressBlock | None:
        if not source:
            return None
        prefix = "  " if state.model.marker_committed else "• "
        state.model.marker_committed = True
        return ProgressBlock(
            self._block_key(state),
            (
                ProgressRow(
                    source,
                    "normal",
                    format="markdown",
                    prefix=prefix,
                ),
            ),
        )

    def _flow_terminal_rows(
        self,
        state: StepState,
        event: StepEnd,
    ) -> tuple[ProgressRow, ...]:
        statement = state.statement
        assert statement is not None
        if event.kind == "loop":
            rows = list(
                loop_terminal_rows(
                    event,
                    statement=statement,
                    observed_iterations=state.loop.iterations,
                    error=self._error_text(event.error),
                )
            )
        elif event.status == "canceled" and state.cancellation_reported:
            rows = []
        elif isinstance(
            statement,
            MapStmt | StormStmt | KeepStmt | DropStmt | RankStmt,
        ):
            rows = list(
                collection_terminal_rows(
                    statement,
                    event,
                    error=self._error_text(event.error),
                )
            )
        elif isinstance(event.error, Pointer):
            rows = []
        else:
            rows = list(flow_terminal_rows(event, error=self._error_text(event.error)))
        facts = self._flow_facts(state, event)
        if facts:
            rows.append(ProgressRow(f"  {' · '.join(facts)}"))
        if rows:
            rows.append(ProgressRow(""))
        return tuple(rows)

    def _par_terminal_rows(
        self,
        state: StepState,
        event: StepEnd,
    ) -> tuple[ProgressRow, ...]:
        statement = state.statement
        assert statement is not None
        tone = (
            "error"
            if event.status == "failed"
            else "warning"
            if event.status == "canceled"
            else "progress"
        )
        if event.status == "succeeded":
            rows = list(
                collection_terminal_rows(
                    statement,
                    event,
                    fallback_total=(
                        state.par.total_items
                        if state.par.total_items is not None
                        else state.par.child_count
                    ),
                )
            )
        else:
            rows = [ProgressRow(f"• {self._par_terminal_text(state, event)}", tone)]
        if event.status == "failed":
            rows.extend(self._failed_lane_rows(state))
            if error := self._error_text(event.error):
                rows.append(ProgressRow(""))
                rows.extend(flow_error_rows(error))
        facts = self._flow_facts(state, event)
        if facts:
            rows.append(ProgressRow(f"  {' · '.join(facts)}"))
        rows.append(ProgressRow(""))
        return tuple(rows)

    def _failed_lane_rows(self, state: StepState) -> tuple[ProgressRow, ...]:
        if not state.par.lanes:
            return ()
        lane_width = len(str(max(state.par.lanes)))
        item_width = len(str(max(lane.item for lane in state.par.lanes.values())))
        rows: list[ProgressRow] = []
        for lane_index, lane in sorted(state.par.lanes.items()):
            if lane.status != "failed":
                continue
            prefix = f"  {lane_index:>{lane_width}} | #{lane.item:>{item_width}} | "
            terminal = lane.terminal or ("• failed",)
            rows.append(ProgressRow(f"{prefix}{terminal[0]}", "error"))
            continuation = " " * (len(prefix) + 2)
            rows.extend(
                ProgressRow(f"{continuation}{line}", "error") for line in terminal[1:]
            )
        return tuple(rows)

    def _run_error_block(self, run: RunState, error: str) -> ProgressBlock:
        owner = self._steps.get(run.begin.parent) if run.begin.parent else None
        boundaries = ()
        if owner is not None:
            boundaries = self._claim_boundaries(f"run:{run.begin.run}", owner)
        rows = (
            *self._rows_for_boundaries(boundaries),
            *flow_error_rows(self._error_text(error)),
        )
        self._commit_boundaries(boundaries)
        return ProgressBlock(f"run:{run.begin.run}", rows)

    def _commit_block(
        self,
        state: StepState,
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
            if not state.is_flow:
                rows = (
                    *self._rows_for_boundaries(state.boundaries),
                    *trace_live_rows(
                        state.begin,
                        state.model.pending if state.begin.kind == "model" else "",
                        marker_committed=(
                            state.model.marker_committed
                            if state.begin.kind == "model"
                            else False
                        ),
                    ),
                )
                if rows:
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

    def _par_live_rows(self, state: StepState) -> tuple[ProgressRow, ...]:
        rows = [
            ProgressRow(f"• running · {self._par_counts(state, live=True)}", "active")
        ]
        if not state.par.lanes:
            return tuple(rows)
        lane_width = len(str(max(state.par.lanes)))
        item_width = len(str(max(lane.item for lane in state.par.lanes.values())))
        for lane_index, lane in sorted(state.par.lanes.items()):
            rows.append(
                ProgressRow(
                    f"  {lane_index:>{lane_width}} | #{lane.item:>{item_width}} | "
                    f"{lane.activity}",
                    "active",
                )
            )
        return tuple(rows)

    def _par_counts(self, state: StepState, *, live: bool) -> str:
        facts = []
        par = state.par
        if par.total_items is not None or par.succeeded_children or not par.child_count:
            succeeded = str(par.succeeded_children)
            if par.total_items is not None:
                succeeded = f"{succeeded}/{par.total_items}"
            facts.append(f"{succeeded} succeeded")
        if par.failed_children:
            facts.append(f"{par.failed_children} failed")
        if live and par.active_children:
            status = "canceling" if par.terminating else "active"
            facts.append(f"{par.active_children} {status}")
        if par.canceled_children:
            facts.append(f"{par.canceled_children} canceled")
        return " · ".join(facts) or "0 succeeded"

    def _par_terminal_text(self, state: StepState, event: StepEnd) -> str:
        par = state.par
        succeeded = str(par.succeeded_children)
        if par.total_items is not None:
            succeeded = f"{succeeded}/{par.total_items}"
        clauses = [f"{succeeded} succeeded"]
        if par.failed_children:
            clauses.append(f"{par.failed_children} failed")
        if par.canceled_children:
            verb = "was" if par.canceled_children == 1 else "were"
            clauses.append(f"{par.canceled_children} {verb} canceled")
        detail = _sentence_list(clauses)
        action = (
            "Parallel execution was canceled"
            if event.status == "canceled"
            else "Parallel execution stopped"
        )
        return f"{action}: {detail}"

    def _flow_facts(self, state: StepState, event: StepEnd) -> list[str]:
        if not state.metrics.has_activity:
            return []
        return state.metrics.facts(
            duration=elapsed(state.begin.started_at, event.finished_at),
            include_runs=True,
        )

    def _claim_boundaries(
        self,
        block_key: str,
        target: StepState,
    ) -> tuple[str, ...]:
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
        target: StepState,
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
                        ProgressRow(f"[{step.ordinal}] {statement_header(statement)}"),
                        ProgressRow(""),
                    ),
                )
            )
            if not isinstance(statement, RepeatStmt | SettleStmt):
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
                if not isinstance(statement, RepeatStmt):
                    continue
                label = until_header(statement)
                candidates.append(
                    (until_key, (ProgressRow(f"<?> {label}"), ProgressRow("")))
                )
        return candidates

    def _flow_chain(self, target: StepState) -> list[StepState]:
        chain: list[StepState] = []
        current: StepState | None = target if target.statement is not None else None
        if current is None:
            parent = self._parent_flow_path(target.begin.step)
            current = self._steps.get(parent) if parent is not None else None
        while current is not None:
            chain.append(current)
            parent = self._parent_flow_path(current.begin.step)
            current = self._steps.get(parent) if parent is not None else None
        chain.reverse()
        return chain

    def _flow_ancestors_in_run(self, path: StepPath) -> list[StepState]:
        ancestors: list[StepState] = []
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
        chain: list[StepState],
        repeat_index: int,
        target: StepState,
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
        parent = self._steps.get(path)
        if parent is None or parent.begin.kind != "loop":
            parent = self._steps.get(path.parent) if path.parent is not None else None
        if parent is None:
            run = self._runs.get(path.run)
            parent = (
                self._steps.get(run.begin.parent)
                if run is not None and run.begin.parent is not None
                else None
            )
        if parent is not None and parent.begin.kind == "loop":
            parent.loop.iterations = max(
                parent.loop.iterations,
                occurrence.iteration.index + 1,
            )

    def _set_lane_activity(
        self,
        owner: LaneOwner,
        activity: str,
    ) -> None:
        lane = self._lane_state(owner)
        if lane is not None:
            lane.activity = one_line(activity)

    def _set_lane_terminal(
        self,
        owner: LaneOwner,
        terminal: tuple[str, ...],
        *,
        status: RunStatus,
    ) -> None:
        lane = self._lane_state(owner)
        if lane is not None:
            lane.terminal = terminal
            lane.terminal_status = status
            if terminal:
                lane.activity = one_line(" · ".join(terminal))

    def _lane_state(self, owner: LaneOwner) -> LaneState | None:
        par = self._steps.get(owner.step)
        if par is None:
            return None
        lane = par.par.lanes.get(owner.lane)
        return lane if lane is not None and lane.run_id == owner.run_id else None

    def _start_canceling(
        self,
        state: StepState,
    ) -> None:
        state.par.terminating = True
        for lane in state.par.lanes.values():
            if lane.active:
                lane.activity = "• canceling"

    def _mark_cancellation_reported(self, path: StepPath) -> None:
        current: StepPath | None = path
        seen: set[StepPath] = set()
        while current is not None and current not in seen:
            seen.add(current)
            state = self._steps.get(current)
            if state is not None:
                state.cancellation_reported = True
            run = self._runs.get(current.run)
            if run is not None:
                run.cancellation_reported = True
            current = self._parent_flow_path(current)

    @staticmethod
    def _lane_for_run(state: StepState, run_id: str) -> LaneState | None:
        return next(
            (lane for lane in state.par.lanes.values() if lane.run_id == run_id),
            None,
        )

    @staticmethod
    def _direct_lane_owner(
        path: StepPath,
        occurrence: Occurrence | None,
        run_id: str,
    ) -> LaneOwner:
        if occurrence is None or occurrence.lane is None or occurrence.item is None:
            raise _PresentationError(
                f"parallel child Run requires lane and item occurrence for {path}"
            )
        return LaneOwner(
            path,
            occurrence.lane.index,
            occurrence.item.index,
            run_id,
        )

    def _active_step(self, path: StepPath) -> StepState:
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
        return error.strip() if isinstance(error, str) else ""

    @staticmethod
    def _is_generic_cancellation(error: str) -> bool:
        return error.casefold().strip(" .:!") in {
            "canceled",
            "cancelled",
            "run canceled",
            "run cancelled",
            "operation canceled",
            "operation cancelled",
        }

    @staticmethod
    def _block_key(state: StepState) -> str:
        prefix = "par" if state.begin.kind == "par" else "step"
        return f"{prefix}:{state.begin.step}"

    def _diagnostic_block(self, message: str) -> ProgressBlock:
        boundaries = tuple(self._boundary_rows)
        rows = (
            *self._rows_for_boundaries(boundaries),
            *flow_error_rows(message),
        )
        self._commit_boundaries(boundaries)
        return ProgressBlock("run:diagnostic", rows)


def _sentence_list(values: list[str]) -> str:
    if len(values) < 2:
        return "".join(values)
    if len(values) == 2:
        return " and ".join(values)
    return f"{', '.join(values[:-1])}, and {values[-1]}"
