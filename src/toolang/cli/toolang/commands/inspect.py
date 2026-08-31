"""Read-only execution subject inspection."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Annotated, Literal, cast

import click
import typer
from rich import box
from rich.cells import cell_len
from rich.console import Console, RenderableType
from rich.table import Table
from rich.text import Text

from toolang.cli.common.human_values import (
    human_scalar_text,
    human_value_renderable,
)
from toolang.cli.common.execution_progress.formatting import (
    elapsed as _format_elapsed,
    one_line as _one_line,
    token_fact as _token_fact,
)
from toolang.execution.history import RunHistory
from toolang.execution.inspection import (
    ChildOccurrenceTotals,
    InspectedRun,
    InspectedStep,
)
from toolang.execution.records import (
    ControlRecord,
    RunRecord,
    StepRecord,
    StoredModelStepGiven,
    ThreadRecord,
    model_call_to_data,
)
from toolang.execution.schemas import (
    Record,
    RecordSelection,
    record_kind,
    record_to_data,
)
from toolang.execution.store import RunStore
from toolang.execution.types import (
    CollectionStepNoted,
    Local,
    ModelStepNoted,
    Occurrence,
    Pointer,
    ToolStepGiven,
    TypedPointer,
    local_to_protocol_data,
    validate_runtime_value,
)

from ...common.execution import open_execution
from ...common.output import echo_table

_SubjectKind = Literal[
    "agent",
    "thread",
    "control",
    "run",
    "step",
    "field",
    "controls",
    "threads",
    "runs",
    "steps",
]
_ProjectionKind = Literal["records", "fields", "value", "tree", "call"]


@dataclass(frozen=True, slots=True)
class _InspectSubject:
    kind: _SubjectKind
    selection: RecordSelection | None = None
    records: tuple[Record, ...] = ()
    scope: str | None = None
    inspected: tuple[InspectedRun | InspectedStep, ...] = ()


@dataclass(frozen=True, slots=True)
class _SubjectTransition:
    source: _SubjectKind
    name: str
    target: _SubjectKind
    load: Callable[[RunStore, _InspectSubject], _InspectSubject]
    supports: Callable[[_InspectSubject], bool] = lambda _subject: True


@dataclass(frozen=True, slots=True)
class _ProjectorTransition:
    source: _SubjectKind
    name: str
    project: Callable[[RunStore, _InspectSubject], object]
    render: Callable[[Console, _InspectSubject, object], None]
    supports: Callable[[_InspectSubject], bool] = lambda _subject: True


@dataclass(frozen=True, slots=True)
class _InspectQuery:
    subjects: tuple[str, ...]
    root_pointer: Pointer | None
    projector: str | None


@dataclass(frozen=True, slots=True)
class _InspectProjection:
    kind: _ProjectionKind
    subject: _InspectSubject
    value: object


@dataclass(frozen=True, slots=True)
class _ProjectedValue:
    json: object
    human: object


def _load_threads(store: RunStore, _source: _InspectSubject) -> _InspectSubject:
    return _InspectSubject(
        kind="threads",
        records=tuple(store.list_threads()),
    )


def _load_controls(store: RunStore, _source: _InspectSubject) -> _InspectSubject:
    return _InspectSubject(
        kind="controls",
        records=store.list_controls(),
    )


def _load_runs(store: RunStore, source: _InspectSubject) -> _InspectSubject:
    thread_id = (
        source.selection.pointer.record
        if source.kind == "thread" and source.selection is not None
        else None
    )
    inspected = store.inspect_runs(thread_id=thread_id)
    return _InspectSubject(
        kind="runs",
        records=tuple(item.record for item in inspected),
        scope=thread_id,
        inspected=inspected,
    )


def _load_steps(store: RunStore, source: _InspectSubject) -> _InspectSubject:
    if source.selection is None:  # pragma: no cover - registry source guarantees this
        raise RuntimeError("run subject has no record selection")
    run_id = source.selection.pointer.record
    inspected = store.inspect_steps(run_id=run_id)
    return _InspectSubject(
        kind="steps",
        records=tuple(item.record for item in inspected),
        scope=run_id,
        inspected=inspected,
    )


def _load_child_runs(store: RunStore, source: _InspectSubject) -> _InspectSubject:
    step = _selected_step(source)
    inspected = store.inspect_child_runs(parent=step)
    return _InspectSubject(
        kind="runs",
        records=tuple(item.record for item in inspected),
        scope=str(step.path),
        inspected=inspected,
    )


def _load_child_steps(store: RunStore, source: _InspectSubject) -> _InspectSubject:
    step = _selected_step(source)
    inspected = store.inspect_child_steps(parent=step)
    return _InspectSubject(
        kind="steps",
        records=tuple(item.record for item in inspected),
        scope=str(step.path),
        inspected=inspected,
    )


def _selected_step(source: _InspectSubject) -> StepRecord:
    if source.selection is None or not isinstance(source.selection.record, StepRecord):
        raise RuntimeError("step subject has no Step record")
    return source.selection.record


def _project_model_call(store: RunStore, source: _InspectSubject) -> object:
    step = _selected_step(source)
    data = model_call_to_data(store.rebuild_model_call(step))
    return _ProjectedValue(json=data, human=data)


def _project_tool_call(_store: RunStore, source: _InspectSubject) -> object:
    step = _selected_step(source)
    if not isinstance(step.given, ToolStepGiven):
        raise ValueError(f"step is not a tool call: {step.path}")
    call = step.given.call
    data = {
        "tool_call_id": call.tool_call_id,
        "call_id": call.call_id,
        "name": call.name,
        "input": dict(call.input),
    }
    return _ProjectedValue(json=data, human=data)


def _project_structural_tree(store: RunStore, source: _InspectSubject) -> object:
    from toolang.execution.trees import build_execution_tree, tree_to_data

    if source.selection is None:
        raise RuntimeError("structural projection has no record selection")
    record = source.selection.record
    root = record.id if isinstance(record, RunRecord) else _selected_step(source).path
    tree = build_execution_tree(store.load_execution_snapshot(root=root))
    return _ProjectedValue(json=tree_to_data(tree), human=tree)


def _project_step_call(store: RunStore, source: _InspectSubject) -> object:
    step = _selected_step(source)
    if step.kind == "model":
        return _project_model_call(store, source)
    if step.kind == "tool":
        return _project_tool_call(store, source)
    if step.kind in {"run", "par", "loop"}:
        return _project_structural_tree(store, source)
    raise click.UsageError(f"{step.path} does not support projector call")


def _render_model_call(
    console: Console,
    subject: _InspectSubject,
    value: object,
) -> None:
    for renderable in _model_call_renderables(
        value,
        step=_selected_step(subject),
        result_parts=_model_step_result_parts(subject),
    ):
        console.print(renderable, soft_wrap=True)


def _render_step_call(
    console: Console,
    subject: _InspectSubject,
    value: object,
) -> None:
    step = _selected_step(subject)
    if step.kind == "model":
        _render_model_call(console, subject, value)
        return
    if step.kind == "tool":
        _render_tool_call(console, subject, value)
        return
    _render_execution_tree(console, subject, value)


def _model_step_result_parts(
    subject: _InspectSubject,
) -> tuple[Mapping[str, object], ...] | None:
    if subject.selection is None or not isinstance(
        subject.selection.record, StepRecord
    ):
        raise RuntimeError("step subject has no Step record")  # pragma: no cover
    output = subject.selection.record.output
    if output is None:
        return None
    value = local_to_protocol_data(output)["value"]
    if not isinstance(value, list):  # pragma: no cover - model output is Part[]
        raise TypeError("model Step output is not a Part array")
    if not all(isinstance(part, Mapping) for part in value):  # pragma: no cover
        raise TypeError("model Step output contains a non-Part value")
    return tuple(cast(Mapping[str, object], part) for part in value)


def _render_tool_call(
    console: Console,
    subject: _InspectSubject,
    value: object,
) -> None:
    step = _selected_step(subject)
    if not isinstance(step.given, ToolStepGiven) or not isinstance(value, Mapping):
        raise TypeError("tool call projector returned an invalid value")
    data = cast(Mapping[str, object], value)
    lines = list(_call_summary_renderables(step, title="Tool Call"))
    _append_model_call_section(lines, "Invocation", width=80)
    tool_call_id = str(data.get("tool_call_id") or "")
    call_id = str(data.get("call_id") or "")
    lines.append(Text(f"Plugin       {step.given.plugin}"))
    lines.append(Text(f"Tool-call ID {tool_call_id}"))
    if call_id and call_id != tool_call_id:
        lines.append(Text(f"Call ID      {call_id}"))
    lines.append(
        Text(
            _tool_invocation(
                _display_tool_name(str(data.get("name") or "tool")),
                data.get("input"),
            )
        )
    )
    result = _tool_step_result(step)
    if result is not None:
        _append_model_call_section(
            lines,
            "Result",
            width=80,
        )
        lines.extend(_tool_part_renderables(result, result=True, index=0))
    for line in lines:
        console.print(line, soft_wrap=True)


def _tool_step_result(step: StepRecord) -> Mapping[str, object] | None:
    if step.output is None:
        return None
    value = local_to_protocol_data(step.output)["value"]
    candidates = value if isinstance(value, list) else [value]
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        result = cast(Mapping[str, object], item)
        if result.get("type") == "tool_result":
            return result
    return None


def _render_execution_tree(
    console: Console,
    _subject: _InspectSubject,
    value: object,
) -> None:
    from toolang.execution.trees import ExecutionTree

    if not isinstance(value, ExecutionTree):
        raise TypeError("structural projector returned an invalid tree")
    children: dict[str | None, list[str]] = {}
    parents = {node.pointer: node.parent for node in value.nodes}
    for node in value.nodes:
        children.setdefault(node.parent, []).append(node.pointer)
    last = {pointer for pointers in children.values() for pointer in pointers[-1:]}
    steps = {
        str(record.path): record
        for record in value.records
        if isinstance(record, StepRecord)
    }

    rows: list[tuple[Text | str, ...]] = []
    for node in value.nodes:
        activity = _tree_activity(node.record_kind, node.step_kind, node.operation)
        occur = _occurrence_index_label(node.occur)
        if node.record_kind == "step":
            step = steps.get(node.pointer)
            if step is None:  # pragma: no cover - typed tree invariant
                raise RuntimeError(f"tree Step record is missing: {node.pointer}")
            occur = _step_occurrence_label(
                step,
                fallback=_tree_child_occurrence_totals(node.pointer, value.nodes),
            )
        rows.append(
            (
                _tree_node_label(node.pointer, node.parent, parents, last),
                _status_activity(node.status, activity),
                occur,
            )
        )
        error = value.resolve_error(node.error)
        if error is not None:
            bounded = _one_line(error)[:240]
            rows.append(
                (
                    "",
                    Text(f"error: {bounded}", style="red"),
                    "",
                )
            )
    _render_execution_table(
        console,
        ("NODE", "ACTIVITY", "OCCUR"),
        rows,
    )


def _render_execution_table(
    console: Console,
    headers: Sequence[str],
    rows: Sequence[Sequence[Text | str]],
) -> None:
    """Render execution pointers without terminal-width truncation."""

    normalized = tuple(
        tuple(cell if isinstance(cell, Text) else Text(cell) for cell in row)
        for row in rows
    )
    minimum_width = sum(
        max(
            (
                cell_len(header),
                *(cell_len(row[index].plain) for row in normalized),
            )
        )
        for index, header in enumerate(headers)
    ) + 2 * len(headers)

    table = Table(
        box=box.HORIZONTALS,
        header_style="",
        show_lines=False,
        pad_edge=False,
        collapse_padding=True,
        width=minimum_width if minimum_width > console.width else None,
    )
    for header in headers:
        table.add_column(header, no_wrap=True, overflow="ignore")
    for row in normalized:
        table.add_row(*row)
    console.print(table, crop=False)


def _echo_execution_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Text | str]],
) -> None:
    _render_execution_table(Console(highlight=False), headers, rows)


def _tree_node_label(
    pointer: str,
    parent: str | None,
    parents: Mapping[str, str | None],
    last: set[str],
) -> str:
    if parent is None:
        return pointer
    ancestors: list[str] = []
    current = parent
    while current is not None:
        ancestors.append(current)
        current = parents[current]
    guides = "".join(
        "   " if ancestor in last else "│  " for ancestor in reversed(ancestors[:-1])
    )
    branch = "└─ " if pointer in last else "├─ "
    return f"{guides}{branch}{pointer}"


def _tree_activity(
    record_kind: str,
    step_kind: str | None,
    operation: str,
) -> str:
    if record_kind == "run":
        return _runnable_activity(operation)
    if step_kind is None:  # pragma: no cover - typed tree invariant
        return operation
    content = operation
    prefix = f"{step_kind} "
    if content.startswith(prefix):
        content = content[len(prefix) :]
    if step_kind == "run":
        content = _replace_runnable_tokens(content)
    return f"[{step_kind}]".ljust(7) + f" {content}"


def _replace_runnable_tokens(operation: str) -> str:
    return " ".join(
        _runnable_activity(word) if word.partition(":")[0] in {"flow", "agic"} else word
        for word in operation.split()
    )


def _runnable_activity(runnable: str) -> str:
    kind, separator, name = runnable.partition(":")
    if separator and kind in {"flow", "agic"} and name:
        return f"<{kind}>".ljust(7) + f" {name}"
    return "<?>".ljust(7) + f" {runnable}"


def _occurrence_index_label(occur: Occurrence | None) -> str:
    if occur is None:
        return ""
    facts = []
    item = occur.item
    lane = occur.lane
    iteration = occur.iteration
    if item is not None:
        facts.append(f"item {item.index + 1}")
    if lane is not None:
        facts.append(f"lane {lane.index + 1}")
    if iteration is not None:
        total = f"/{iteration.count}" if iteration.count is not None else ""
        facts.append(f"iteration {iteration.index + 1}{total} {iteration.phase}")
    return " · ".join(facts)


def _status_activity(status: object, activity: str) -> Text:
    value = str(status or "")
    marker, style = {
        "pending": ("•", "dim"),
        "running": ("•", "cyan"),
        "succeeded": ("✔", "green"),
        "failed": ("✖", "red"),
        "canceled": ("✖", "yellow"),
    }.get(value, ("?", "dim"))
    rendered = Text()
    rendered.append(marker, style=style)
    if activity:
        rendered.append(f" {activity}")
    return rendered


def _step_occurrence_label(
    step: StepRecord,
    *,
    fallback: ChildOccurrenceTotals,
) -> str:
    facts = [_occurrence_index_label(step.occur)]
    items = fallback.items
    lanes = fallback.lanes
    if isinstance(step.noted, CollectionStepNoted):
        items = step.noted.total_items
    elif step.kind in {"run", "par"}:
        count = getattr(step.given, "count", None)
        if isinstance(count, int) and not isinstance(count, bool):
            items = count
    if step.kind == "par":
        configured_lanes = getattr(step.given, "lanes", None)
        if isinstance(configured_lanes, int) and not isinstance(configured_lanes, bool):
            lanes = configured_lanes
    if items is not None:
        facts.append(f"{items} {_plural(items, 'item')}")
    if lanes is not None:
        facts.append(f"{lanes} {_plural(lanes, 'lane')}")
    return " · ".join(fact for fact in facts if fact)


def _tree_child_occurrence_totals(
    pointer: str,
    nodes: Sequence[object],
) -> ChildOccurrenceTotals:
    child_occurrences = [
        getattr(node, "occur")
        for node in nodes
        if getattr(node, "parent") == pointer and getattr(node, "record_kind") == "run"
    ]

    def consistent_count(name: Literal["item", "lane"]) -> int | None:
        if not child_occurrences:
            return None
        positions = [
            getattr(occur, name) if isinstance(occur, Occurrence) else None
            for occur in child_occurrences
        ]
        if any(position is None for position in positions):
            return None
        counts = {position.count for position in positions if position is not None}
        return counts.pop() if len(counts) == 1 else None

    return ChildOccurrenceTotals(
        items=consistent_count("item"),
        lanes=consistent_count("lane"),
    )


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else f"{noun}s"


INSPECT_SUBJECT_TRANSITIONS: tuple[_SubjectTransition, ...] = (
    _SubjectTransition("agent", "threads", "threads", _load_threads),
    _SubjectTransition("agent", "runs", "runs", _load_runs),
    _SubjectTransition("agent", "controls", "controls", _load_controls),
    _SubjectTransition("thread", "runs", "runs", _load_runs),
    _SubjectTransition("run", "steps", "steps", _load_steps),
    _SubjectTransition(
        "step",
        "runs",
        "runs",
        _load_child_runs,
        lambda subject: _selected_step(subject).kind in {"run", "par", "loop"},
    ),
    _SubjectTransition(
        "step",
        "steps",
        "steps",
        _load_child_steps,
        lambda subject: _selected_step(subject).kind == "loop",
    ),
)

INSPECT_PROJECTORS: tuple[_ProjectorTransition, ...] = (
    _ProjectorTransition(
        "run",
        "tree",
        _project_structural_tree,
        _render_execution_tree,
    ),
    _ProjectorTransition(
        "step",
        "call",
        _project_step_call,
        _render_step_call,
        lambda subject: (
            _selected_step(subject).kind in {"model", "tool", "run", "par", "loop"}
        ),
    ),
)

_STATIC_SUBJECT_NAMES = frozenset(
    transition.name for transition in INSPECT_SUBJECT_TRANSITIONS
)
_PROJECTOR_NAMES = frozenset(projector.name for projector in INSPECT_PROJECTORS)


def _inspect_subject_help() -> str:
    roots = ", ".join(_allowed_transitions(_InspectSubject(kind="agent")))
    relations = "; ".join(
        f"{'LOOP_STEP' if transition.source == 'step' and transition.name == 'steps' else transition.source.upper()} "
        f"{transition.name}"
        for transition in INSPECT_SUBJECT_TRANSITIONS
        if transition.source != "agent"
    )
    projectors = "; ".join(
        f"{projector.source.upper()} {projector.name}"
        for projector in INSPECT_PROJECTORS
    )
    return (
        f"Subject chain. Root subjects: {roots}, or POINTER. "
        f"Relations: {relations}. Projectors: {projectors}. "
        "Run tree is a durable structural snapshot; Step call is the "
        "Step-owned historical call."
    )


def inspect_command(
    ctx: typer.Context,
    subjects: Annotated[
        list[str],
        typer.Argument(
            metavar="SUBJECT...",
            help=_inspect_subject_help(),
        ),
    ],
    human: Annotated[
        bool,
        typer.Option("--human", help="Render human-readable output (default)."),
    ] = False,
    json_view: Annotated[
        bool, typer.Option("--json", help="Render exact canonical JSON.")
    ] = False,
) -> None:
    """Inspect execution subjects."""

    if human and json_view:
        raise click.UsageError("--human and --json are mutually exclusive")
    query = _parse_inspect_query(subjects)
    with open_execution(ctx, required=True) as resources:
        if resources is None:  # pragma: no cover - required=True guarantees this
            raise RuntimeError("execution resources were not opened")
        try:
            subject = _resolve_inspect_subject(resources.store, query)
            projection = _resolve_inspect_projection(
                resources.store,
                subject,
                query.projector,
            )
            if json_view:
                _render_projection_json(projection)
            else:
                _render_projection_human(resources.store, projection)
        except click.UsageError:
            raise
        except (TypeError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc


def _parse_inspect_query(values: Sequence[str]) -> _InspectQuery:
    tokens = tuple(values)
    if not tokens:
        raise click.UsageError("inspect requires a subject")
    projector = (
        tokens[-1] if len(tokens) > 1 and tokens[-1] in _PROJECTOR_NAMES else None
    )
    subjects = tokens[:-1] if projector is not None else tokens
    head = subjects[0]
    if head in _STATIC_SUBJECT_NAMES:
        root_pointer = None
    else:
        try:
            root_pointer = Pointer(head)
        except (TypeError, ValueError) as exc:
            raise click.UsageError(str(exc)) from exc
    return _InspectQuery(
        subjects=subjects,
        root_pointer=root_pointer,
        projector=projector,
    )


def _resolve_inspect_subject(store: RunStore, query: _InspectQuery) -> _InspectSubject:
    current = _InspectSubject(kind="agent")
    for index, token in enumerate(query.subjects):
        transition = _subject_transition(current, token)
        if transition is not None:
            resolved = transition.load(store, current)
            if resolved.kind != transition.target:
                raise RuntimeError(
                    f"inspect transition {transition.source} {transition.name} "
                    f"resolved {resolved.kind}, expected {transition.target}"
                )
            current = resolved
            continue
        if current.kind == "agent" and index == 0 and query.root_pointer is not None:
            selection = store.select_pointer(query.root_pointer)
            current = _InspectSubject(
                kind=(
                    "field"
                    if query.root_pointer.field
                    else record_kind(selection.record)
                ),
                selection=selection,
            )
            continue
        raise _invalid_child(current, token)
    return current


def _subject_transition(
    source: _InspectSubject | _SubjectKind,
    name: str,
) -> _SubjectTransition | None:
    source_kind = source.kind if isinstance(source, _InspectSubject) else source
    if isinstance(source, _InspectSubject) and name not in _available_names(source):
        return None
    return next(
        (
            transition
            for transition in INSPECT_SUBJECT_TRANSITIONS
            if transition.source == source_kind and transition.name == name
        ),
        None,
    )


def _allowed_transitions(
    source: _InspectSubject | _SubjectKind,
) -> tuple[str, ...]:
    source_kind = source.kind if isinstance(source, _InspectSubject) else source
    return tuple(
        transition.name
        for transition in INSPECT_SUBJECT_TRANSITIONS
        if transition.source == source_kind
        and (not isinstance(source, _InspectSubject) or transition.supports(source))
    )


def _allowed_projectors(
    source: _InspectSubject | _SubjectKind,
) -> tuple[str, ...]:
    source_kind = source.kind if isinstance(source, _InspectSubject) else source
    return tuple(
        projector.name
        for projector in INSPECT_PROJECTORS
        if projector.source == source_kind
        and (not isinstance(source, _InspectSubject) or projector.supports(source))
    )


def _available_names(source: _InspectSubject) -> tuple[str, ...]:
    """Return every relation and explicit projector valid for one subject."""

    return (*_allowed_transitions(source), *_allowed_projectors(source))


def _projector_transition(
    source: _InspectSubject | _SubjectKind,
    name: str,
) -> _ProjectorTransition | None:
    source_kind = source.kind if isinstance(source, _InspectSubject) else source
    if isinstance(source, _InspectSubject) and name not in _available_names(source):
        return None
    return next(
        (
            projector
            for projector in INSPECT_PROJECTORS
            if projector.source == source_kind and projector.name == name
        ),
        None,
    )


def _invalid_child(subject: _InspectSubject, token: str) -> click.UsageError:
    allowed = _available_names(subject)
    label = _inspect_subject_label(subject)
    if allowed:
        return click.UsageError(
            f"invalid child subject {token!r} after {label}; "
            f"allowed: {', '.join(allowed)}"
        )
    return click.UsageError(f"{label} does not accept a child subject: {token!r}")


def _apply_projector(
    store: RunStore,
    subject: _InspectSubject,
    name: str,
) -> object:
    projector = _projector_transition(subject, name)
    if projector is None:
        raise _invalid_child(subject, name)
    return projector.project(store, subject)


def _resolve_inspect_projection(
    store: RunStore,
    subject: _InspectSubject,
    projector: str | None,
) -> _InspectProjection:
    if projector is not None:
        value = _apply_projector(store, subject, projector)
        if not isinstance(value, _ProjectedValue):
            raise RuntimeError(f"inspect projector {projector} returned no projection")
        return _InspectProjection(
            kind=cast(_ProjectionKind, projector),
            subject=subject,
            value=value,
        )
    if subject.selection is None:
        return _InspectProjection(
            kind="records",
            subject=subject,
            value=subject.records,
        )
    return _InspectProjection(
        kind=_implicit_pointer_projector(subject.selection),
        subject=subject,
        value=subject.selection.value,
    )


def _implicit_pointer_projector(
    selected: RecordSelection,
) -> Literal["fields", "value"]:
    if isinstance(selected.runtime, Local) or selected.is_pointer:
        return "value"
    if selected.render_type in {"Part", "Part[]"}:
        return "value"
    if isinstance(selected.value, Mapping | list) and bool(selected.value):
        return "fields"
    return "value"


def _inspect_subject_label(subject: _InspectSubject) -> str:
    if subject.selection is not None:
        return str(subject.selection.pointer)
    return subject.kind


def _echo_json(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _render_projection_json(projection: _InspectProjection) -> None:
    if projection.kind == "records":
        records = cast(tuple[Record, ...], projection.value)
        _echo_json([record_to_data(record) for record in records])
        return
    if projection.kind in {"fields", "value"}:
        _echo_json(projection.value)
        return
    projected = projection.value
    if not isinstance(projected, _ProjectedValue):
        raise RuntimeError("explicit projection has no typed value")
    _echo_json(projected.json)


def _render_projection_human(
    store: RunStore,
    projection: _InspectProjection,
) -> None:
    if projection.kind == "records":
        _render_collection(store, projection.subject)
        return
    if projection.kind == "fields":
        selected = projection.subject.selection
        if selected is None:  # pragma: no cover - projection invariant
            raise RuntimeError("fields projection has no selection")
        _render_pointer(store, selected, projector="fields")
        return
    if projection.kind == "value":
        selected = projection.subject.selection
        if selected is None:  # pragma: no cover - projection invariant
            raise RuntimeError("value projection has no selection")
        _render_pointer(store, selected, projector="value")
        return
    _render_explicit_projection(
        projection.subject,
        projection.kind,
        projection.value,
    )


def _render_collection(
    store: RunStore,
    subject: _InspectSubject,
) -> None:
    history = RunHistory(store)
    if subject.kind == "controls":
        controls = cast(tuple[ControlRecord, ...], subject.records)
        rows = [
            (
                str(Pointer.control(control.target, control.index)),
                control.kind,
                _display_status(control.status),
                control.created_at,
            )
            for control in controls
        ]
        echo_table(("CONTROL", "KIND", "STATUS", "CREATED"), rows)
        return
    if subject.kind == "threads":
        records = cast(tuple[ThreadRecord, ...], subject.records)
        items = history.describe_threads(records)
        rows = [
            (
                item.id,
                _truncate(item.title, width=48),
                str(item.run_count),
                item.status,
                item.updated_at,
            )
            for item in items
        ]
        echo_table(("THREAD", "TITLE", "RUNS", "STATUS", "UPDATED"), rows)
        return
    if subject.kind == "runs":
        items = tuple(
            item for item in subject.inspected if isinstance(item, InspectedRun)
        )
        if subject.scope is not None and "." in subject.scope:
            rows = [
                (
                    item.record.id,
                    _runnable_activity(item.runnable),
                    _display_status(item.record.status),
                    str(item.step_count),
                    item.record.created_at,
                )
                for item in items
            ]
            _echo_execution_table(
                (
                    "RUN",
                    "RUNNABLE",
                    "STATUS",
                    "STEPS",
                    "CREATED",
                ),
                rows,
            )
            return
        if subject.scope is not None:
            rows = [
                (
                    item.record.id,
                    _runnable_activity(item.runnable),
                    _display_status(item.record.status),
                    str(item.step_count),
                    str(item.record.parent) if item.record.parent is not None else "-",
                    item.record.created_at,
                )
                for item in items
            ]
            _echo_execution_table(
                (
                    "RUN",
                    "RUNNABLE",
                    "STATUS",
                    "STEPS",
                    "PARENT STEP",
                    "CREATED",
                ),
                rows,
            )
            return
        rows = [
            (
                item.record.id,
                _runnable_activity(item.runnable),
                _display_status(item.record.status),
                str(item.step_count),
                item.record.thread,
                str(item.record.parent) if item.record.parent is not None else "-",
                item.record.created_at,
            )
            for item in items
        ]
        _echo_execution_table(
            (
                "RUN",
                "RUNNABLE",
                "STATUS",
                "STEPS",
                "THREAD",
                "PARENT STEP",
                "CREATED",
            ),
            rows,
        )
        return
    if subject.kind == "steps":
        items = tuple(
            item for item in subject.inspected if isinstance(item, InspectedStep)
        )
        child_steps = subject.scope is not None and "." in subject.scope
        if child_steps:
            rows = [
                (
                    str(item.record.path),
                    _status_activity(
                        item.record.status,
                        _tree_activity("step", item.record.kind, item.operation),
                    ),
                    str(item.child_run_count),
                    str(item.child_step_count),
                    item.record.created_at,
                    _step_occurrence_label(
                        item.record,
                        fallback=item.child_occurrence_totals,
                    ),
                )
                for item in items
            ]
            _echo_execution_table(
                (
                    "STEP",
                    "ACTIVITY",
                    "CHILD RUNS",
                    "CHILD STEPS",
                    "CREATED",
                    "OCCUR",
                ),
                rows,
            )
            return
        rows = [
            (
                str(item.record.path),
                _status_activity(
                    item.record.status,
                    _tree_activity("step", item.record.kind, item.operation),
                ),
                str(item.child_run_count),
                str(item.child_step_count),
                str(item.record.parent) if item.record.parent is not None else "-",
                item.record.created_at,
                _step_occurrence_label(
                    item.record,
                    fallback=item.child_occurrence_totals,
                ),
            )
            for item in items
        ]
        _echo_execution_table(
            (
                "STEP",
                "ACTIVITY",
                "CHILD RUNS",
                "CHILD STEPS",
                "PARENT STEP",
                "CREATED",
                "OCCUR",
            ),
            rows,
        )
        return
    raise RuntimeError(f"unsupported collection subject: {subject.kind}")


def _render_explicit_projection(
    subject: _InspectSubject,
    projector: str,
    value: object,
) -> None:
    transition = _projector_transition(subject, projector)
    if transition is None:  # pragma: no cover - projection was already applied
        raise RuntimeError(
            f"missing human renderer for {subject.kind} projector {projector}"
        )
    console = Console(highlight=False)
    if not isinstance(value, _ProjectedValue):
        raise RuntimeError("explicit projection has no typed value")
    transition.render(console, subject, value.human)


def _model_call_renderables(
    value: object,
    *,
    step: StepRecord | None = None,
    result_parts: Sequence[Mapping[str, object]] | None = None,
    section_width: int = 80,
) -> tuple[Text, ...]:
    if not isinstance(value, Mapping):  # pragma: no cover - projector is canonical
        raise TypeError("model Step call projector returned a non-object value")
    data = cast(Mapping[str, object], value)

    lines = list(
        _call_summary_renderables(step, title="Model Call") if step is not None else ()
    )
    instructions = data.get("instructions")
    if isinstance(instructions, str) and instructions:
        _append_model_call_section(lines, "Instructions", width=section_width)
        lines.append(Text(instructions))

    messages = data.get("messages")
    message_count = len(messages) if isinstance(messages, list) else 0
    if isinstance(messages, list) and messages:
        _append_model_call_section(
            lines,
            "Messages",
            width=section_width,
        )
        for offset, message in enumerate(messages):
            if not isinstance(message, Mapping):  # pragma: no cover - canonical data
                continue
            message_data = cast(Mapping[str, object], message)
            if offset:
                lines.append(Text())
            role = str(message_data.get("role") or "message")
            _append_model_message(
                lines,
                index=message_count - offset,
                role=role,
                parts=message_data.get("parts"),
            )

    tools = data.get("tools")
    if isinstance(tools, list) and tools:
        _append_model_call_section(
            lines,
            "Tools",
            width=section_width,
        )
        for index, tool in enumerate(tools):
            if not isinstance(tool, Mapping):  # pragma: no cover - canonical data
                continue
            tool_data = cast(Mapping[str, object], tool)
            if index:
                lines.append(Text())
            name = str(tool_data.get("name") or "unnamed")
            description = tool_data.get("description")
            lines.extend(
                (
                    Text(
                        f"[{index}] {_tool_signature(name, tool_data.get('parameters'))}",
                        style="dim",
                    ),
                    Text(),
                    Text(description)
                    if isinstance(description, str) and description
                    else Text("No description.", style="dim italic"),
                )
            )

    output_schema = data.get("output_schema")
    if output_schema is not None:
        _append_model_call_section(lines, "Output Contract", width=section_width)
        lines.extend(
            Text(line)
            for line in json.dumps(
                output_schema,
                ensure_ascii=False,
                indent=2,
            ).splitlines()
        )

    continuation = data.get("cont")
    if continuation is not None:
        _append_model_call_section(lines, "Continuation", width=section_width)
        lines.extend(_structured_renderables(continuation))

    if result_parts is not None:
        _append_model_call_section(
            lines,
            "Result",
            width=section_width,
        )
        _append_model_message(
            lines,
            index="=",
            role="assistant",
            parts=result_parts,
        )
    return tuple(lines)


def _append_model_message(
    lines: list[Text],
    *,
    index: int | Literal["="],
    role: str,
    parts: object,
) -> None:
    heading = Text(f"[{index}] {role}", style="dim")
    lines.extend((heading, Text()))
    if not isinstance(parts, Sequence) or isinstance(parts, str) or not parts:
        lines.append(Text("No content.", style="dim italic"))
        return
    for part_index, part in enumerate(parts):
        if not isinstance(part, Mapping):  # pragma: no cover - canonical data
            continue
        if part_index:
            lines.append(Text())
        lines.extend(
            _message_part_renderables(
                cast(Mapping[str, object], part),
                index=part_index,
                multipart=len(parts) > 1,
            )
        )


def _call_summary_renderables(
    step: StepRecord,
    *,
    title: str,
) -> tuple[Text, ...]:
    lines: list[Text] = []
    _append_model_call_section(lines, title, width=80)
    lines.append(Text(f"Step         {step.path}"))
    if isinstance(step.given, StoredModelStepGiven):
        lines.append(Text(f"Model        {step.given.model}"))
    elif isinstance(step.given, ToolStepGiven):
        lines.append(Text(f"Tool         {_display_tool_name(step.given.call.name)}"))
    status = Text("Status       ")
    status.append_text(_status_activity(step.status, step.status))
    lines.append(status)
    elapsed = _format_elapsed(step.started_at, step.finished_at or "")
    if elapsed:
        lines.append(Text(f"Elapsed      {elapsed}"))
    if isinstance(step.noted, ModelStepNoted):
        usage = _model_usage_fact(step.noted)
        if usage:
            lines.append(Text(f"Usage        {usage}"))
    return tuple(lines)


def _model_usage_fact(noted: ModelStepNoted) -> str:
    accounting = noted.accounting
    if accounting is not None:
        return _token_fact(accounting.input_tokens, accounting.output_tokens)
    if noted.tokens is not None:
        return _token_fact(noted.tokens.input, noted.tokens.output)
    return ""


def _message_part_renderables(
    part: Mapping[str, object],
    *,
    index: int,
    multipart: bool,
) -> tuple[Text, ...]:
    part_type = str(part.get("type") or "part")
    if part_type == "text":
        text = part.get("text")
        return (Text(text if isinstance(text, str) else ""),)
    if part_type == "tool_call":
        return _tool_part_renderables(part, result=False, index=index)
    if part_type == "tool_result":
        return _tool_part_renderables(part, result=True, index=index)
    label = part_type.replace("_", " ").title()
    details = dict(part)
    details.pop("type", None)
    prefix = f"[{index}] " if multipart else ""
    return (
        Text(f"{prefix}{label}", style="dim"),
        *_structured_renderables(details),
    )


def _text_preview(value: str, *, width: int = 160) -> Text:
    preview = _truncate(_one_line(value), width=width)
    line_count = value.count("\n") + 1
    facts: list[str] = []
    if line_count > 1:
        facts.append(f"{line_count} lines")
    if len(preview) < len(_one_line(value)) or line_count > 1:
        facts.append(f"{len(value)} chars")
    output = Text(preview)
    if facts:
        output.append(f" · {' · '.join(facts)}", style="dim")
    return output


def _append_model_call_section(
    lines: list[Text],
    title: str,
    *,
    fact: str | None = None,
    width: int,
) -> None:
    if lines:
        lines.append(Text())
    line_width = max(1, width)
    heading = Text()
    heading.append(title, style="bold")
    if fact is not None:
        heading.append(f" {fact}", style="dim")
    remaining = line_width - cell_len(heading.plain)
    if remaining > 0:
        fill = "░" * remaining
        if remaining > 1:
            fill = f" {'░' * (remaining - 1)}"
        heading.append(fill, style="dim")
    boundary = "░" * line_width
    lines.extend(
        (
            Text(boundary, style="dim"),
            heading,
            Text(boundary, style="dim"),
            Text(),
        )
    )


def _tool_signature(name: str, parameters: object) -> str:
    display_name = _display_tool_name(name)
    if not isinstance(parameters, Mapping):
        return f"{display_name}()"
    schema = cast(Mapping[str, object], parameters)
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return f"{display_name}()"
    property_schemas = cast(Mapping[object, object], properties)
    required_value = schema.get("required")
    required = (
        {str(item) for item in required_value}
        if isinstance(required_value, list)
        else set()
    )
    params = []
    for key, value in property_schemas.items():
        param_name = str(key)
        optional = "" if param_name in required else "?"
        params.append(f"{param_name}{optional}: {_schema_type(value)}")
    return f"{display_name}({', '.join(params)})"


def _display_tool_name(name: str) -> str:
    return name.replace("__", ".", 1)


def _schema_type(value: object) -> str:
    if not isinstance(value, Mapping):
        return "any"
    schema = cast(Mapping[str, object], value)
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return " | ".join(_structured_scalar(item) for item in enum)
    for union_key in ("anyOf", "oneOf"):
        variants = schema.get(union_key)
        if isinstance(variants, list) and variants:
            return " | ".join(_schema_type(item) for item in variants)
    type_value = schema.get("type")
    if isinstance(type_value, list):
        return " | ".join(str(item) for item in type_value)
    if type_value == "array":
        return f"{_schema_type(schema.get('items'))}[]"
    if isinstance(type_value, str):
        return type_value
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference:
        return reference.rsplit("/", maxsplit=1)[-1]
    return "any"


def _tool_part_renderables(
    part: Mapping[str, object],
    *,
    result: bool,
    index: int,
) -> tuple[Text, ...]:
    type_name = "ToolResultPart" if result else "ToolCallPart"
    tool_call_id = str(part.get("tool_call_id") or "unknown")
    header = f"<[[ {type_name}({index}), id={tool_call_id}"
    output: object = part.get("output", {})
    if result:
        result_facts, output = _tool_result_presentation(output)
        if result_facts:
            header += f", {', '.join(result_facts)}"
    lines = [Text(header, style="dim")]
    if not result:
        name = _display_tool_name(str(part.get("tool_name") or "unnamed"))
        lines.append(Text(_tool_invocation(name, part.get("input", {}))))
    if result and output != {}:
        lines.extend(_structured_renderables(output))
    reasoning = part.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        lines.extend((Text(), Text("Reason", style="bold"), Text(reasoning)))
    error = part.get("error")
    if isinstance(error, str) and error:
        lines.extend((Text(), Text("Error", style="bold"), Text(error)))
    lines.append(Text("]]>", style="dim"))
    return tuple(lines)


def _tool_result_presentation(output: object) -> tuple[tuple[str, ...], object]:
    if not isinstance(output, Mapping):  # pragma: no cover - canonical ToolResultPart
        return (), output
    body = dict(cast(Mapping[object, object], output))
    facts = []
    for key in ("status", "exit_code", "ok"):
        if key in body:
            facts.append(f"{key}={_structured_scalar(body.pop(key))}")
    return tuple(facts), body


def _tool_invocation(name: str, value: object) -> str:
    if not isinstance(value, Mapping):
        return f"{name}()"
    arguments = cast(Mapping[object, object], value)
    rendered = ", ".join(
        f"{key}: {_argument_value(item)}" for key, item in arguments.items()
    )
    return f"{name}({rendered})"


def _argument_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def _structured_renderables(
    value: object,
    *,
    style: str = "",
) -> tuple[Text, ...]:
    return tuple(Text(line, style=style) for line in _structured_text_lines(value))


def _structured_text_lines(value: object, *, indent: int = 0) -> tuple[str, ...]:
    prefix = " " * indent
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if not mapping:
            return (f"{prefix}{{}}",)
        lines: list[str] = []
        for key, item in mapping.items():
            lines.extend(_structured_item_lines(str(key), item, indent=indent))
        return tuple(lines)
    if isinstance(value, list):
        items = cast(list[object], value)
        if not items:
            return (f"{prefix}[]",)
        lines = []
        for index, item in enumerate(items):
            lines.extend(_structured_item_lines(f"[{index}]", item, indent=indent))
        return tuple(lines)
    if isinstance(value, str) and "\n" in value:
        return tuple(f"{prefix}{line}" for line in value.splitlines())
    return (f"{prefix}{_structured_scalar(value)}",)


def _structured_item_lines(
    label: str,
    value: object,
    *,
    indent: int,
) -> tuple[str, ...]:
    prefix = " " * indent
    if isinstance(value, str) and "\n" in value:
        body = tuple(f"{' ' * (indent + 2)}{line}" for line in value.splitlines())
        return (f"{prefix}{label}:", *body)
    if isinstance(value, Mapping | list):
        return (
            f"{prefix}{label}:",
            *_structured_text_lines(value, indent=indent + 2),
        )
    return (f"{prefix}{label}: {_structured_scalar(value)}",)


def _structured_scalar(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class _HumanValue:
    data: object
    runtime: object
    render_type: str
    resolved: bool
    raw: bool = False


def _render_pointer(
    store: RunStore,
    selected: RecordSelection,
    *,
    projector: Literal["fields", "value"],
) -> None:
    console = Console(highlight=False)
    if projector == "fields":
        _render_human_rows(
            console,
            _raw_human_children(selected),
            base=selected.pointer,
            pointer_heading="FIELD",
        )
        return
    root = _human_value(store, selected)
    block = _human_block(root)
    if block is not None:
        console.print(block)
    else:
        console.print(Text(_human_summary(root)), soft_wrap=True)


def _raw_human_children(
    selected: RecordSelection,
) -> Iterable[tuple[RecordSelection, _HumanValue]]:
    keys: Iterable[str | int]
    if isinstance(selected.value, Mapping):
        keys = (cast(str, key) for key in selected.value)
    else:
        keys = range(len(cast(Sequence[object], selected.value)))
    for key in keys:
        child = selected.child(key)
        yield (
            child,
            _HumanValue(
                data=child.value,
                runtime=child.value,
                render_type=child.type_name,
                resolved=False,
                raw=True,
            ),
        )


def _human_value(store: RunStore, selected: RecordSelection) -> _HumanValue:
    data = selected.value
    runtime = selected.runtime
    render_type = selected.render_type
    resolved = False
    expected: list[str] = []
    visited: list[Pointer] = []

    while True:
        if isinstance(runtime, Local):
            protocol = local_to_protocol_data(runtime)
            local_type = runtime.type
            data = protocol["value"]
            runtime = runtime.value
            render_type = local_type
        if isinstance(runtime, TypedPointer):
            expected.append(runtime.type)
            pointer = runtime.pointer
        elif isinstance(runtime, Pointer):
            pointer = runtime
        else:
            break
        if pointer in visited:
            cycle = " -> ".join(str(item) for item in (*visited, pointer))
            raise ValueError(f"Pointer cycle: {cycle}")
        visited.append(pointer)
        target = store.select_pointer(pointer)
        data = target.value
        runtime = target.runtime
        render_type = target.render_type
        resolved = True

    for type_name in expected:
        validate_runtime_value(runtime, type_name, path=f"Pointer {selected.pointer}")
    return _HumanValue(data, runtime, render_type, resolved)


def _render_human_rows(
    console: Console,
    rows: Iterable[tuple[RecordSelection, _HumanValue]],
    *,
    base: Pointer | None = None,
    pointer_heading: str | None = None,
) -> None:
    compact: list[tuple[str, str, RenderableType]] = []
    heading = pointer_heading or ("FIELD" if base is not None else "POINTER")
    for selected, value in rows:
        pointer = str(selected.pointer)
        if base is not None:
            pointer = pointer.removeprefix(str(base))
        type_label = _human_type_label(value.render_type)
        if value.resolved:
            type_label = f"*{type_label}"
        rendered = None if value.raw else _human_block(value)
        compact.append(
            (
                pointer,
                type_label,
                (
                    rendered
                    if rendered is not None
                    else _raw_human_summary(value.data)
                    if value.raw
                    else _human_summary(value)
                ),
            )
        )
    _print_human_table(console, compact, pointer_heading=heading)


def _print_human_table(
    console: Console,
    rows: Sequence[tuple[str, str, RenderableType]],
    *,
    pointer_heading: str = "POINTER",
) -> None:
    if not rows:
        return
    minimum_width = (
        max(
            cell_len(pointer_heading),
            *(cell_len(pointer) for pointer, _type, _value in rows),
        )
        + max(
            cell_len("TYPE"),
            *(cell_len(type_name) for _pointer, type_name, _value in rows),
        )
        + 8
        + cell_len("VALUE")
    )
    table = Table(
        box=box.HORIZONTALS,
        header_style="",
        show_lines=False,
        collapse_padding=True,
        show_header=True,
        pad_edge=False,
        width=minimum_width if minimum_width > console.width else None,
    )
    table.add_column(pointer_heading, no_wrap=True, overflow="ignore")
    table.add_column("TYPE", no_wrap=True, overflow="ignore")
    table.add_column("VALUE")
    for pointer, type_name, value in rows:
        table.add_row(pointer, type_name, value)
    console.print(table, crop=False)


def _human_type_label(type_name: str) -> str:
    members = type_name.split(" | ")
    if "None" not in members:
        return type_name
    present = [member for member in members if member != "None"]
    if len(present) == len(members) or not present:
        return type_name
    inner = " | ".join(present)
    return f"{inner}?" if len(present) == 1 else f"({inner})?"


def _human_block(value: _HumanValue) -> RenderableType | None:
    return human_value_renderable(value.runtime, value.render_type)


def _human_summary(value: _HumanValue) -> str:
    data = value.data
    natural = human_scalar_text(value.runtime, value.render_type)
    if natural is not None:
        return natural
    if isinstance(data, str):
        return data
    if isinstance(data, Mapping):
        items = [f"{key}: {_nested_summary(value)}" for key, value in data.items()]
        return _truncate("{" + ", ".join(items) + "}", width=120)
    if isinstance(data, list):
        return "[]" if not data else f"[{len(data)} items]"
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _raw_human_summary(value: object) -> RenderableType:
    if isinstance(value, str):
        return _text_preview(value)
    return _truncate(
        json.dumps(value, ensure_ascii=False, separators=(", ", ": ")),
        width=160,
    )


def _nested_summary(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return "{}" if not value else "{...}"
    if isinstance(value, list):
        return "[]" if not value else f"[{len(value)} items]"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _display_status(value: object) -> str:
    return str(value or "")


def _truncate(value: object, *, width: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return f"{text[: width - 3].rstrip()}..."
