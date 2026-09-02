"""Historical execution tree construction and aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from .accounting import token_meter_quantity
from .inspection import ExecutionSnapshot, step_operation
from .records import (
    ControlRecord,
    RunRecord,
    StepRecord,
    occurrence_to_data,
)
from .schemas import Record, select_record
from .types import (
    ExecutionError,
    Local,
    ModelStepNoted,
    Occurrence,
    Pointer,
    StepKind,
    StepPath,
    TypedPointer,
)


@dataclass(frozen=True, slots=True)
class TreeMetrics:
    """Durable subtree metrics for one execution node."""

    runs: int
    model_calls: int
    tool_calls: int
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    usage_complete: bool
    reasoning_complete: bool
    cost_usd: str | None
    cost_complete: bool
    cost_approximate: bool


@dataclass(frozen=True, slots=True)
class ExecutionTreeNode:
    """One flat depth-first historical execution node."""

    pointer: str
    record_kind: Literal["run", "step"]
    step_kind: StepKind | None
    parent: str | None
    depth: int
    operation: str
    status: str
    occur: Occurrence | None
    started_at: str
    finished_at: str | None
    error: ExecutionError | None
    metrics: TreeMetrics


@dataclass(frozen=True, slots=True)
class ExecutionTree:
    """A validated flat tree plus its frozen snapshot for Human resolution."""

    nodes: tuple[ExecutionTreeNode, ...]
    records: tuple[Record, ...]
    entries: tuple[ControlRecord, ...]

    def resolve_error(self, error: ExecutionError | None) -> str | None:
        """Best-effort resolve one error Pointer without leaving the snapshot."""

        if error is None or isinstance(error, str):
            return error
        records = {_record_pointer(record): record for record in self.records}
        controls = {
            str(Pointer.control(control.target, control.index)): control
            for control in self.entries
        }
        selected: str | Pointer | object = error
        seen: set[Pointer] = set()
        while isinstance(selected, Pointer):
            if selected in seen:
                return f"{selected} (unresolved cycle)"
            seen.add(selected)
            record = records.get(selected.record) or controls.get(selected.record)
            if record is None:
                return f"{selected} (unresolved)"
            try:
                value = select_record(record, selected)
            except (TypeError, ValueError):
                return f"{selected} (unresolved)"
            selected = value.runtime
            if isinstance(selected, Local):
                selected = selected.value
            if isinstance(selected, TypedPointer):
                selected = selected.pointer
        return selected if isinstance(selected, str) else f"{error} (unresolved)"


@dataclass(slots=True)
class _MetricAccumulator:
    runs: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    token_known: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_known: int = 0
    reasoning_tokens: int = 0
    cost_known: int = 0
    cost_complete: int = 0
    cost_usd: Decimal = Decimal(0)
    cost_approximate: bool = False

    def add(self, other: _MetricAccumulator) -> None:
        self.runs += other.runs
        self.model_calls += other.model_calls
        self.tool_calls += other.tool_calls
        self.token_known += other.token_known
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_known += other.reasoning_known
        self.reasoning_tokens += other.reasoning_tokens
        self.cost_known += other.cost_known
        self.cost_complete += other.cost_complete
        self.cost_usd += other.cost_usd
        self.cost_approximate = self.cost_approximate or other.cost_approximate


def build_execution_tree(snapshot: ExecutionSnapshot) -> ExecutionTree:
    """Validate and project one durable execution snapshot."""

    runs = {run.id: run for run in snapshot.runs}
    steps = {step.path: step for step in snapshot.steps}
    if len(runs) != len(snapshot.runs) or len(steps) != len(snapshot.steps):
        raise ValueError("execution tree contains duplicate records")
    entries = {(item.target, item.index): item for item in snapshot.entries}
    operations: dict[str, str] = {}
    for run in snapshot.runs:
        if run.control.target != run.id:
            raise ValueError(
                f"run preparation control not found: {run.id}@{run.control.index}"
            )
        entry = entries.get((run.control.target, run.control.index))
        if entry is None:
            raise ValueError(
                f"run preparation control not found: {run.id}@{run.control.index}"
            )
        payload = entry.payload
        runnable = getattr(payload, "runnable", None)
        if not isinstance(runnable, str) or not runnable:
            raise ValueError(
                f"run preparation control not found: {run.id}@{run.control.index}"
            )
        operations[run.id] = runnable
    for step in snapshot.steps:
        operations[str(step.path)] = step_operation(step)

    root_pointer = _record_pointer(snapshot.root)
    if isinstance(snapshot.root, RunRecord):
        if runs.get(snapshot.root.id) != snapshot.root:
            raise ValueError(f"execution tree root is missing: {snapshot.root.id}")
    elif steps.get(snapshot.root.path) != snapshot.root:
        raise ValueError(f"execution tree root is missing: {snapshot.root.path}")

    top_steps: dict[str, list[StepRecord]] = {}
    nested_steps: dict[StepPath, list[StepRecord]] = {}
    for step in snapshot.steps:
        if step.parent is None:
            top_steps.setdefault(step.run_id, []).append(step)
        else:
            nested_steps.setdefault(step.parent, []).append(step)
    child_runs: dict[StepPath, list[RunRecord]] = {}
    for run in snapshot.runs:
        if run.id == root_pointer:
            continue
        if run.parent is not None:
            child_runs.setdefault(run.parent, []).append(run)

    for owner, children in top_steps.items():
        children.sort(key=lambda step: step.path.indices)
        if owner not in runs and not (
            isinstance(snapshot.root, StepRecord) and owner == snapshot.root.run_id
        ):
            raise ValueError(f"execution tree Step owner is missing: {owner}")

    adjacency: dict[str, tuple[RunRecord | StepRecord, ...]] = {}
    for run in snapshot.runs:
        adjacency[run.id] = tuple(top_steps.get(run.id, ()))
    for step in snapshot.steps:
        adjacency[str(step.path)] = _ordered_step_children(
            step,
            nested_steps.get(step.path, ()),
            child_runs.get(step.path, ()),
        )

    visiting: set[str] = set()
    visited: set[str] = set()
    order: list[tuple[RunRecord | StepRecord, str | None, int]] = []
    stack: list[tuple[RunRecord | StepRecord, str | None, int, bool]] = [
        (snapshot.root, None, 0, False)
    ]
    while stack:
        record, parent, depth, exiting = stack.pop()
        pointer = _record_pointer(record)
        if exiting:
            visiting.remove(pointer)
            visited.add(pointer)
            continue
        if pointer in visiting:
            raise ValueError(f"execution tree contains a cycle: {pointer}")
        if pointer in visited:
            raise ValueError(f"execution tree contains duplicate ownership: {pointer}")
        visiting.add(pointer)
        order.append((record, parent, depth))
        stack.append((record, parent, depth, True))
        stack.extend(
            (child, pointer, depth + 1, False)
            for child in reversed(adjacency.get(pointer, ()))
        )
    all_pointers = set(runs) | {str(path) for path in steps}
    remaining = sorted(all_pointers - visited)
    if remaining:
        raise ValueError(
            "execution tree contains an orphan or cycle: " + ", ".join(remaining)
        )

    accumulators: dict[str, _MetricAccumulator] = {}
    for record, _parent, _depth in reversed(order):
        pointer = _record_pointer(record)
        accumulator = _record_metrics(record)
        for child in adjacency.get(pointer, ()):
            accumulator.add(accumulators[_record_pointer(child)])
        accumulators[pointer] = accumulator

    nodes = tuple(
        _node_from_record(
            record,
            parent=parent,
            depth=depth,
            operation=operations[_record_pointer(record)],
            accumulator=accumulators[_record_pointer(record)],
        )
        for record, parent, depth in order
    )
    return ExecutionTree(
        nodes=nodes,
        records=(*snapshot.runs, *snapshot.steps),
        entries=snapshot.entries,
    )


def tree_to_data(tree: ExecutionTree) -> list[dict[str, object]]:
    """Serialize a tree as its stable flat depth-first JSON projection."""

    return [
        {
            "pointer": node.pointer,
            "record_kind": node.record_kind,
            "step_kind": node.step_kind,
            "parent": node.parent,
            "depth": node.depth,
            "operation": node.operation,
            "status": node.status,
            "occur": occurrence_to_data(node.occur),
            "started_at": node.started_at,
            "finished_at": node.finished_at,
            "error": (
                str(node.error) if isinstance(node.error, Pointer) else node.error
            ),
            "metrics": {
                "runs": node.metrics.runs,
                "model_calls": node.metrics.model_calls,
                "tool_calls": node.metrics.tool_calls,
                "input_tokens": node.metrics.input_tokens,
                "output_tokens": node.metrics.output_tokens,
                "reasoning_tokens": node.metrics.reasoning_tokens,
                "usage_complete": node.metrics.usage_complete,
                "reasoning_complete": node.metrics.reasoning_complete,
                "cost_usd": node.metrics.cost_usd,
                "cost_complete": node.metrics.cost_complete,
                "cost_approximate": node.metrics.cost_approximate,
            },
        }
        for node in tree.nodes
    ]


def _ordered_step_children(
    parent: StepRecord,
    same_run: tuple[StepRecord, ...] | list[StepRecord],
    runs: tuple[RunRecord, ...] | list[RunRecord],
) -> tuple[RunRecord | StepRecord, ...]:
    nested = tuple(same_run)
    child_runs = tuple(runs)
    if parent.kind not in {"run", "par", "loop"} and (nested or child_runs):
        raise ValueError(f"{parent.kind} Step cannot own execution: {parent.path}")
    if parent.kind != "loop" and nested:
        raise ValueError(f"{parent.kind} Step cannot own nested Steps: {parent.path}")
    if parent.kind == "run":
        if len(child_runs) > 1:
            raise ValueError(f"run Step has multiple child Runs: {parent.path}")
        return child_runs
    if parent.kind == "par":
        for run in child_runs:
            if run.occur is None or run.occur.item is None or run.occur.lane is None:
                raise ValueError(
                    f"parallel child Run requires item and lane occurrence: {run.id}"
                )
        return tuple(sorted(child_runs, key=lambda run: (run.occur.item.index, run.id)))
    if parent.kind == "loop":
        children: tuple[RunRecord | StepRecord, ...] = (*nested, *child_runs)
        for child in children:
            if child.occur is None or child.occur.iteration is None:
                raise ValueError(
                    f"loop child requires iteration occurrence: {_record_pointer(child)}"
                )
        return tuple(sorted(children, key=_loop_child_order))
    return ()


def _loop_child_order(record: RunRecord | StepRecord) -> tuple[object, ...]:
    occur = record.occur
    if occur is None or occur.iteration is None:  # pragma: no cover - validated first
        raise ValueError("loop child has no iteration occurrence")
    iteration = occur.iteration
    phase = 0 if iteration.phase == "body" else 1
    if isinstance(record, StepRecord):
        return (iteration.index, phase, 0, record.path.indices, record.created_at)
    item = occur.item.index if occur.item is not None else 0
    return (iteration.index, phase, 1, (item,), record.created_at, record.id)


def _record_metrics(record: RunRecord | StepRecord) -> _MetricAccumulator:
    accumulator = _MetricAccumulator(runs=1 if isinstance(record, RunRecord) else 0)
    if not isinstance(record, StepRecord):
        return accumulator
    if record.kind == "tool":
        accumulator.tool_calls = 1
        return accumulator
    if record.kind != "model":
        return accumulator
    accumulator.model_calls = 1
    noted = record.noted if isinstance(record.noted, ModelStepNoted) else None
    if noted is None:
        return accumulator
    if noted.accounting is not None:
        accumulator.token_known = 1
        accumulator.input_tokens = noted.accounting.input_tokens
        accumulator.output_tokens = noted.accounting.output_tokens
        reasoning = token_meter_quantity(noted.accounting, "output.reasoning")
        if reasoning is not None:
            accumulator.reasoning_known = 1
            accumulator.reasoning_tokens = reasoning
    elif noted.tokens is not None:
        accumulator.token_known = 1
        accumulator.input_tokens = noted.tokens.input
        accumulator.output_tokens = noted.tokens.output

    cost: str | None = None
    complete = False
    approximate = False
    if noted.accounting is not None:
        selected = noted.accounting.selected
        selected_cost = (
            noted.accounting.reported
            if selected == "reported"
            else noted.accounting.estimate
            if selected == "estimated"
            else None
        )
        if selected_cost is not None and selected_cost.currency.upper() == "USD":
            cost = selected_cost.amount
            complete = selected_cost.complete
            approximate = selected == "estimated"
    if cost is None and noted.cost is not None:
        cost = noted.cost
        complete = True
        approximate = True
    if cost is not None:
        try:
            accumulator.cost_usd = Decimal(cost)
        except InvalidOperation as exc:  # pragma: no cover - record validation
            raise ValueError(f"invalid model cost for {record.path}: {cost}") from exc
        accumulator.cost_known = 1
        accumulator.cost_complete = int(complete)
        accumulator.cost_approximate = approximate
    return accumulator


def _node_from_record(
    record: RunRecord | StepRecord,
    *,
    parent: str | None,
    depth: int,
    operation: str,
    accumulator: _MetricAccumulator,
) -> ExecutionTreeNode:
    is_run = isinstance(record, RunRecord)
    model_calls = accumulator.model_calls
    tokens = accumulator.token_known > 0 or model_calls == 0
    reasoning = accumulator.reasoning_known > 0 or model_calls == 0
    cost = accumulator.cost_known > 0
    return ExecutionTreeNode(
        pointer=_record_pointer(record),
        record_kind="run" if is_run else "step",
        step_kind=record.kind if isinstance(record, StepRecord) else None,
        parent=parent,
        depth=depth,
        operation=operation,
        status=record.status,
        occur=record.occur,
        started_at=record.started_at,
        finished_at=record.finished_at,
        error=record.error,
        metrics=TreeMetrics(
            runs=accumulator.runs - (1 if is_run else 0),
            model_calls=model_calls,
            tool_calls=accumulator.tool_calls,
            input_tokens=accumulator.input_tokens if tokens else None,
            output_tokens=accumulator.output_tokens if tokens else None,
            reasoning_tokens=(accumulator.reasoning_tokens if reasoning else None),
            usage_complete=accumulator.token_known == model_calls,
            reasoning_complete=accumulator.reasoning_known == model_calls,
            cost_usd=_decimal_text(accumulator.cost_usd) if cost else None,
            cost_complete=accumulator.cost_complete == model_calls,
            cost_approximate=accumulator.cost_approximate,
        ),
    )


def _record_pointer(record: Record) -> str:
    if isinstance(record, RunRecord):
        return record.id
    if isinstance(record, StepRecord):
        return str(record.path)
    if isinstance(record, ControlRecord):
        return str(Pointer.control(record.target, record.index))
    return record.id


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"
