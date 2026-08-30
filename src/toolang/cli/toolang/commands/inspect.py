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
from toolang.execution.history import RunHistory
from toolang.execution.records import (
    ControlRecord,
    RunRecord,
    StepRecord,
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
    Local,
    Pointer,
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
_ProjectionKind = Literal["records", "fields", "value", "model-call"]


@dataclass(frozen=True, slots=True)
class _InspectSubject:
    kind: _SubjectKind
    selection: RecordSelection | None = None
    records: tuple[Record, ...] = ()
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class _SubjectTransition:
    source: _SubjectKind
    name: str
    target: _SubjectKind
    load: Callable[[RunStore, _InspectSubject], _InspectSubject]


@dataclass(frozen=True, slots=True)
class _ProjectorTransition:
    source: _SubjectKind
    name: str
    supports: Callable[[_InspectSubject], bool]
    project: Callable[[RunStore, _InspectSubject], object]
    render: Callable[[Console, _InspectSubject, object], None]


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
    return _InspectSubject(
        kind="runs",
        records=tuple(store.list_runs(limit=None, thread_id=thread_id)),
        scope=thread_id,
    )


def _load_steps(store: RunStore, source: _InspectSubject) -> _InspectSubject:
    if source.selection is None:  # pragma: no cover - registry source guarantees this
        raise RuntimeError("run subject has no record selection")
    run_id = source.selection.pointer.record
    return _InspectSubject(
        kind="steps",
        records=tuple(store.list_steps(run_id=run_id)),
        scope=run_id,
    )


def _project_model_call(store: RunStore, source: _InspectSubject) -> object:
    if source.selection is None or not isinstance(source.selection.record, StepRecord):
        raise RuntimeError("step subject has no Step record")  # pragma: no cover
    return model_call_to_data(store.rebuild_model_call(source.selection.record))


def _render_model_call(
    console: Console,
    subject: _InspectSubject,
    value: object,
) -> None:
    for renderable in _model_call_renderables(
        value,
        result_parts=_model_step_result_parts(subject),
    ):
        console.print(renderable, soft_wrap=True)


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


def _supports_model_call(subject: _InspectSubject) -> bool:
    return (
        subject.selection is not None
        and isinstance(subject.selection.record, StepRecord)
        and subject.selection.record.kind == "model"
    )


INSPECT_SUBJECT_TRANSITIONS: tuple[_SubjectTransition, ...] = (
    _SubjectTransition("agent", "threads", "threads", _load_threads),
    _SubjectTransition("agent", "runs", "runs", _load_runs),
    _SubjectTransition("agent", "controls", "controls", _load_controls),
    _SubjectTransition("thread", "runs", "runs", _load_runs),
    _SubjectTransition("run", "steps", "steps", _load_steps),
)

INSPECT_PROJECTORS: tuple[_ProjectorTransition, ...] = (
    _ProjectorTransition(
        "step",
        "model-call",
        _supports_model_call,
        _project_model_call,
        _render_model_call,
    ),
)

_STATIC_SUBJECT_NAMES = frozenset(
    transition.name for transition in INSPECT_SUBJECT_TRANSITIONS
)
_PROJECTOR_NAMES = frozenset(projector.name for projector in INSPECT_PROJECTORS)


def _inspect_subject_help() -> str:
    roots = ", ".join(_allowed_transitions("agent"))
    relations = "; ".join(
        f"{transition.source.upper()} {transition.name}"
        for transition in INSPECT_SUBJECT_TRANSITIONS
        if transition.source != "agent"
    )
    projectors = "; ".join(
        f"{projector.source.upper()} {projector.name}"
        for projector in INSPECT_PROJECTORS
    )
    return (
        f"Subject chain. Root subjects: {roots}, or POINTER. "
        f"Relations: {relations}. Projectors: {projectors}."
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
        transition = _subject_transition(current.kind, token)
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
    source: _SubjectKind,
    name: str,
) -> _SubjectTransition | None:
    return next(
        (
            transition
            for transition in INSPECT_SUBJECT_TRANSITIONS
            if transition.source == source and transition.name == name
        ),
        None,
    )


def _allowed_transitions(source: _SubjectKind) -> tuple[str, ...]:
    return tuple(
        transition.name
        for transition in INSPECT_SUBJECT_TRANSITIONS
        if transition.source == source
    )


def _allowed_projectors(source: _SubjectKind) -> tuple[str, ...]:
    return tuple(
        projector.name for projector in INSPECT_PROJECTORS if projector.source == source
    )


def _available_projectors(subject: _InspectSubject) -> tuple[str, ...]:
    return tuple(
        projector.name
        for projector in INSPECT_PROJECTORS
        if projector.source == subject.kind and projector.supports(subject)
    )


def _projector_transition(
    source: _SubjectKind,
    name: str,
) -> _ProjectorTransition | None:
    return next(
        (
            projector
            for projector in INSPECT_PROJECTORS
            if projector.source == source and projector.name == name
        ),
        None,
    )


def _invalid_child(subject: _InspectSubject, token: str) -> click.UsageError:
    allowed = (*_allowed_transitions(subject.kind), *_available_projectors(subject))
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
    projector = _projector_transition(subject.kind, name)
    if projector is None:
        raise click.UsageError(
            f"{_inspect_subject_label(subject)} does not support projector {name}"
        )
    return projector.project(store, subject)


def _resolve_inspect_projection(
    store: RunStore,
    subject: _InspectSubject,
    projector: str | None,
) -> _InspectProjection:
    if projector is not None:
        return _InspectProjection(
            kind=cast(_ProjectionKind, projector),
            subject=subject,
            value=_apply_projector(store, subject, projector),
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
    _echo_json(projection.value)


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
        records = cast(tuple[RunRecord, ...], subject.records)
        steps_by_run = store.list_steps_for_runs(
            run_ids=tuple(record.id for record in records)
        )
        items = history.describe_runs(records, steps_by_run=steps_by_run)
        if subject.scope is not None:
            rows = [
                (
                    item.id,
                    _truncate(item.summary or item.input_text, width=48),
                    str(len(steps_by_run.get(item.id, ()))),
                    _display_status(item.status),
                    item.created_at,
                )
                for item in items
            ]
            echo_table(
                ("THREAD RUN", "TITLE", "STEPS", "STATUS", "CREATED"),
                rows,
            )
            return
        rows = [
            (
                item.id,
                item.thread_id,
                _truncate(item.summary or item.input_text, width=48),
                str(len(steps_by_run.get(item.id, ()))),
                _display_status(item.status),
                item.created_at,
            )
            for item in items
        ]
        echo_table(
            ("RUN", "THREAD", "TITLE", "STEPS", "STATUS", "CREATED"),
            rows,
        )
        return
    if subject.kind == "steps":
        steps = cast(tuple[StepRecord, ...], subject.records)
        rows = [
            (
                str(step.path),
                step.kind,
                _display_status(step.status),
                step.created_at,
            )
            for step in steps
        ]
        echo_table(("RUN STEP", "KIND", "STATUS", "CREATED"), rows)
        return
    raise RuntimeError(f"unsupported collection subject: {subject.kind}")


def _render_explicit_projection(
    subject: _InspectSubject,
    projector: str,
    value: object,
) -> None:
    transition = _projector_transition(subject.kind, projector)
    if transition is None:  # pragma: no cover - projection was already applied
        raise RuntimeError(
            f"missing human renderer for {subject.kind} projector {projector}"
        )
    console = Console(highlight=False)
    transition.render(console, subject, value)


def _model_call_renderables(
    value: object,
    *,
    result_parts: Sequence[Mapping[str, object]] | None = None,
    section_width: int = 80,
) -> tuple[Text, ...]:
    if not isinstance(value, Mapping):  # pragma: no cover - projector is canonical
        raise TypeError("model-call projector returned a non-object value")
    data = cast(Mapping[str, object], value)

    lines: list[Text] = []
    _append_model_call_section(lines, "Instructions", width=section_width)
    instructions = data.get("instructions")
    lines.append(
        Text(instructions)
        if isinstance(instructions, str) and instructions
        else Text("No instructions.", style="dim italic")
    )

    messages = data.get("messages")
    message_count = len(messages) if isinstance(messages, list) else 0
    _append_model_call_section(
        lines,
        "Messages",
        fact=str(message_count),
        width=section_width,
    )
    if isinstance(messages, list) and messages:
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
    else:
        lines.append(Text("No messages.", style="dim italic"))

    _append_model_call_section(lines, "Output Contract", width=section_width)
    output_schema = data.get("output_schema")
    if output_schema is None:
        lines.append(Text("None", style="dim"))
    else:
        lines.extend(
            Text(line)
            for line in json.dumps(
                output_schema,
                ensure_ascii=False,
                indent=2,
            ).splitlines()
        )

    _append_model_call_section(lines, "Output", width=section_width)
    if result_parts is not None:
        _append_model_message(
            lines,
            index="=",
            role="assistant",
            parts=result_parts,
        )
    else:
        lines.append(Text("No output.", style="dim italic"))

    tools = data.get("tools")
    tool_count = len(tools) if isinstance(tools, list) else 0
    _append_model_call_section(
        lines,
        "Tools",
        fact=str(tool_count),
        width=section_width,
    )
    if isinstance(tools, list) and tools:
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
    else:
        lines.append(Text("No available tools.", style="dim italic"))

    _append_model_call_section(lines, "Continuation", width=section_width)
    continuation = data.get("cont")
    if continuation is None:
        lines.append(Text("No continuation data.", style="dim italic"))
    else:
        lines.extend(_structured_renderables(continuation))
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
    reasoning = part.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        lines.extend((Text(), Text("Reason", style="bold"), Text(reasoning)))
    error = part.get("error")
    if isinstance(error, str) and error:
        lines.extend((Text(), Text("Error", style="bold"), Text(error)))
    elif result and output != {}:
        lines.extend(_structured_renderables(output))
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


def _render_pointer(
    store: RunStore,
    selected: RecordSelection,
    *,
    projector: Literal["fields", "value"],
) -> None:
    console = Console(highlight=False)
    root = _human_value(store, selected)
    if projector == "fields":
        _render_human_rows(
            console,
            _human_children(store, selected),
            base=selected.pointer,
            pointer_heading=_field_projection_heading(selected, root),
        )
        return
    block = _human_block(root)
    if block is not None:
        console.print(block)
    else:
        console.print(Text(_human_summary(root)), soft_wrap=True)


def _field_projection_heading(
    selected: RecordSelection,
    value: _HumanValue,
) -> str:
    source = (
        record_kind(selected.record)
        if not selected.pointer.field
        else _human_type_label(value.render_type)
    )
    return f"{source.upper()} FIELD"


def _human_children(
    store: RunStore,
    selected: RecordSelection,
) -> Iterable[tuple[RecordSelection, _HumanValue]]:
    keys: Iterable[str | int]
    if isinstance(selected.value, Mapping):
        keys = (cast(str, key) for key in selected.value)
    else:
        keys = range(len(cast(Sequence[object], selected.value)))
    for key in keys:
        child = selected.child(key)
        yield child, _human_value(store, child)


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
        rendered = _human_block(value)
        compact.append(
            (
                pointer,
                type_label,
                rendered if rendered is not None else _human_summary(value),
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
