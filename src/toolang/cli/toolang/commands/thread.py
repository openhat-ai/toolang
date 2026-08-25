"""Local thread and run inspection and control commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
from typing import Annotated, Literal, cast

import click
from pydantic import TypeAdapter
import typer

from toolang.base.types.message import Message
from toolang.base.types.run import ModelCall, ToolCall
from toolang.cli.common.policy import (
    resolve_binding_overrides,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.common.layout import AgentLayout
from toolang.execution.calls import resolve_spec
from toolang.execution.executor import RunExecutor, prepare_model_call
from toolang.execution.history import RunHistory
from toolang.execution.model_requests import ModelRequest, build_model_request
from toolang.execution.records import (
    ControlRecord,
    RunRecord,
    StepRecord,
    ThreadRecord,
    ToolStepGiven,
    model_call_to_data,
)
from toolang.execution.schemas import Record, record_to_data
from toolang.execution.threads import ThreadManager
from toolang.execution.types import Pointer, RunStatus, StepPath
from toolang.lang.includes import resolve_file_include
from toolang.lang.input import RunnableInputRaw
from toolang.setup import AgentSetup, SetupWatcher
from toolang.state.state import AgentState
from toolang.state.watcher import StateWatcher

from ...common.context import (
    context_layout,
    context_model_catalog,
    load_runtime_environ,
    user_call,
)
from ...common.execution import ExecutionResources, open_execution
from ...common.execution_progress.config import resolve_progress_max_width
from ...common.output import echo_table
from ...common.script_progress import ScriptRunPresenter


_PREVIEW_RUN_ID = "run_inspect_preview"
_PREVIEW_THREAD_ID = "term_inspect_preview"
_MODEL_HISTORY_LIMIT = 32
_HUMAN_TEXT_LIMIT = 12_000
_TOOL_CALL_ADAPTER = TypeAdapter(ToolCall)


def threads_command(
    ctx: typer.Context,
    origin: Annotated[
        str | None, typer.Option("--origin", help="Filter by origin.")
    ] = None,
    channel: Annotated[
        str | None, typer.Option("--channel", help="Filter by channel.")
    ] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="Filter by thread status.")
    ] = None,
) -> None:
    """List threads from the selected agent's durable execution store."""

    with open_execution(ctx) as resources:
        items = (
            []
            if resources is None
            else RunHistory(resources.store).list_threads(
                origin=origin,
                channel=channel,
                status=status,
            )
        )
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


def runs_command(
    ctx: typer.Context,
    thread: Annotated[
        str | None, typer.Option("--thread", help="Filter by thread id.")
    ] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="Filter by run status.")
    ] = None,
) -> None:
    """List runs from the selected agent's durable execution store."""

    run_status = _run_status(status)
    with open_execution(ctx) as resources:
        items = (
            []
            if resources is None
            else RunHistory(resources.store).list_runs(
                thread_id=thread,
                status=run_status,
            )
        )
    if thread is not None:
        rows = [
            (
                item.id,
                _truncate(item.summary or item.input_text, width=48),
                _display_status(item.status),
                item.created_at,
            )
            for item in items
        ]
        echo_table(("RUN", "TITLE", "STATUS", "CREATED"), rows)
        return
    rows = [
        (
            item.thread_id,
            item.id,
            _truncate(item.summary or item.input_text, width=48),
            _display_status(item.status),
            item.created_at,
        )
        for item in items
    ]
    echo_table(("THREAD", "RUN", "TITLE", "STATUS", "CREATED"), rows)


def inspect_command(
    ctx: typer.Context,
    target: Annotated[
        str | None,
        typer.Argument(help="Pointer to a record or field."),
    ] = None,
    focus: Annotated[
        str | None,
        typer.Option(
            "--focus",
            help="Focus on model_call, model_request, or tool_call.",
        ),
    ] = None,
    json_view: Annotated[
        bool, typer.Option("--json", help="Render inspection data as JSON.")
    ] = False,
    full: Annotated[
        bool, typer.Option("--full", help="Do not truncate human text output.")
    ] = False,
    input_source: Annotated[
        str | None,
        typer.Option(
            "--input",
            metavar="CONTENT",
            help="Set prospective primary input; use - for stdin.",
        ),
    ] = None,
    arguments: Annotated[
        list[str] | None,
        typer.Option(
            "--arg",
            metavar="NAME=CONTENT",
            help="Set a prospective named input. Repeat for another input.",
        ),
    ] = None,
    include_thread: Annotated[
        bool,
        typer.Option(
            "--thread",
            help="Include history when exactly one current thread exists.",
        ),
    ] = False,
    allows: Annotated[
        list[str] | None,
        typer.Option(
            "--allow",
            metavar="DOMAIN=SELECTORS",
            help="Set a prospective capability ceiling. Repeat by domain.",
        ),
    ] = None,
    defaults: Annotated[
        list[str] | None,
        typer.Option(
            "--default",
            metavar="FIELD=VALUE",
            help="Set runnable or model for focused inspection. Repeat by field.",
        ),
    ] = None,
) -> None:
    """Inspect a durable record, field, model call, or provider request.

    A Pointer is RECORD_REF[/FIELD...]. Run ids select runs, dotted run paths
    select steps, ids with ^INDEX select controls, and other ids select threads.
    Field refs use RFC 6901 escaping. Use `too records` for complete schemas.
    """

    pointer = parse_inspect_target(target) if target is not None else None
    selected_focus = _inspect_focus(focus)
    if pointer is None and selected_focus is None:
        typer.echo(ctx.get_help())
        return
    _validate_inspect_options(
        pointer,
        focus=selected_focus,
        json_view=json_view,
        full=full,
        input_source=input_source,
        arguments=arguments or (),
        include_thread=include_thread,
        allows=allows or (),
        defaults=defaults or (),
    )
    execution = (
        open_execution(ctx, required=True)
        if pointer is not None or include_thread
        else nullcontext(None)
    )
    with execution as resources:
        inspected = user_call(
            asyncio.run,
            _inspect_value(
                ctx,
                pointer,
                focus=selected_focus,
                resources=resources,
                input_source=input_source,
                arguments=tuple(arguments or ()),
                include_thread=include_thread,
                allows=tuple(allows or ()),
                defaults=tuple(defaults or ()),
            ),
        )
    if json_view:
        typer.echo(json.dumps(_focused_data(inspected), ensure_ascii=False, indent=2))
        return
    _render_inspected(inspected, pointer=pointer, full=full)


def steer_command(
    ctx: typer.Context,
    run: str = typer.Argument(
        ..., help="Run id to steer. Thread id means its active run."
    ),
    message: str = typer.Argument(..., help="Instruction to steer the run."),
) -> None:
    """Persist one next-step steer control."""

    with open_execution(ctx, required=True, writable=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        run_id = _active_run_id(RunHistory(resources.store), run)
        user_call(
            RunExecutor(resources.store, resources.ids).steer,
            run_id=run_id,
            message=Message.user(message),
            timing="next_step",
        )
    typer.echo(f"steered {run_id}")


def cancel_command(
    ctx: typer.Context,
    run: str = typer.Argument(
        ..., help="Run id to cancel. Thread id means its active run."
    ),
) -> None:
    """Persist one immediate stop control."""

    with open_execution(ctx, required=True, writable=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        run_id = _active_run_id(RunHistory(resources.store), run)
        user_call(
            RunExecutor(resources.store, resources.ids).stop,
            run_id=run_id,
        )
    typer.echo(f"canceled {run_id}")


def retry_command(
    ctx: typer.Context,
    run: str = typer.Argument(
        ...,
        help="Run id to retry. Thread id means its latest visible run.",
    ),
    anchor: Annotated[
        str | None,
        typer.Option(
            "--anchor",
            help="Retry from this canonical or run-local step path.",
        ),
    ] = None,
    allows: Annotated[
        list[str] | None,
        typer.Option("--allow", help="Set DOMAIN=SELECTORS. Repeat by domain."),
    ] = None,
    limit: Annotated[
        list[str] | None,
        typer.Option(
            "--limit",
            help="Set FIELD=VALUE. Repeat for another field.",
        ),
    ] = None,
    defaults: Annotated[
        list[str] | None,
        typer.Option("--default", help="Set model=VALUE for retried work."),
    ] = None,
) -> None:
    """Retry one terminal root run from a durable step boundary."""

    with open_execution(ctx, required=True, writable=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        _thread_id, run_id = _anchor(RunHistory(resources.store), run)
        show_progress = sys.stderr.isatty()
        result = user_call(
            asyncio.run,
            _restart_run(
                resources,
                layout=context_layout(ctx),
                kind="retry",
                source=run_id,
                anchor=user_call(_retry_anchor, run_id, anchor),
                allow_options=allows,
                default_options=defaults,
                limit_options=limit,
                show_progress=show_progress,
                model_catalog=context_model_catalog(ctx),
            ),
        )
    status = _display_status(result.status)
    if not show_progress:
        typer.echo(f"retried {result.id}: {status}")
    if result.status != "succeeded":
        raise typer.Exit(1)


def rerun_command(
    ctx: typer.Context,
    run: str = typer.Argument(
        ...,
        help="Run id to rerun. Thread id means its latest visible run.",
    ),
    allows: Annotated[
        list[str] | None,
        typer.Option("--allow", help="Set DOMAIN=SELECTORS. Repeat by domain."),
    ] = None,
    limit: Annotated[
        list[str] | None,
        typer.Option(
            "--limit",
            help="Set FIELD=VALUE. Repeat for another field.",
        ),
    ] = None,
    defaults: Annotated[
        list[str] | None,
        typer.Option("--default", help="Set model=VALUE for the new run."),
    ] = None,
) -> None:
    """Start a new root run from one terminal source invocation."""

    with open_execution(ctx, required=True, writable=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        _thread_id, source = _anchor(RunHistory(resources.store), run)
        show_progress = sys.stderr.isatty()
        result = user_call(
            asyncio.run,
            _restart_run(
                resources,
                layout=context_layout(ctx),
                kind="rerun",
                source=source,
                anchor=None,
                allow_options=allows,
                default_options=defaults,
                limit_options=limit,
                show_progress=show_progress,
                model_catalog=context_model_catalog(ctx),
            ),
        )
    status = _display_status(result.status)
    if not show_progress:
        typer.echo(f"reran {source} as {result.id}: {status}")
    if result.status != "succeeded":
        raise typer.Exit(1)


def rewind_command(
    ctx: typer.Context,
    point: str = typer.Argument(
        ...,
        help="Run id to rewind before. Thread id means rewind before its latest run.",
    ),
    chat: Annotated[
        bool, typer.Option("--chat", help="Open chat on the rewound thread.")
    ] = False,
) -> None:
    """Rewind one idle thread before a terminal anchor run."""

    with open_execution(ctx, required=True, writable=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        history = RunHistory(resources.store)
        thread_id, run_id = _anchor(history, point)
        user_call(
            ThreadManager(resources.store, resources.ids).rewind,
            thread_id=thread_id,
            run_id=run_id,
        )
    typer.echo(f"rewound {thread_id} before {run_id}")
    if chat:
        _open_chat(ctx, thread_id)


def fork_command(
    ctx: typer.Context,
    point: str = typer.Argument(
        ...,
        help="Run id to fork through. Thread id means fork through its latest run.",
    ),
    chat: Annotated[
        bool, typer.Option("--chat", help="Open chat on the forked thread.")
    ] = False,
) -> None:
    """Fork one thread through a terminal anchor run."""

    with open_execution(ctx, required=True, writable=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        history = RunHistory(resources.store)
        thread_id, run_id = _anchor(history, point)
        forked_id = cast(
            str,
            user_call(
                ThreadManager(resources.store, resources.ids).fork,
                thread_id=thread_id,
                run_id=run_id,
            ),
        )
    typer.echo(f"forked {forked_id} through {run_id}")
    if chat:
        _open_chat(ctx, forked_id)


def parse_inspect_target(target: str) -> Pointer:
    """Parse one inspect Pointer without reading external state."""

    try:
        return Pointer(target.strip())
    except (TypeError, ValueError) as exc:
        raise click.UsageError(f"invalid Pointer: {target}") from exc


def _inspect_focus(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in {"model_call", "model_request", "tool_call"}:
        raise click.UsageError(f"unknown inspect focus: {value}")
    return value


def _validate_inspect_options(
    pointer: Pointer | None,
    *,
    focus: str | None,
    json_view: bool,
    full: bool,
    input_source: str | None,
    arguments: Sequence[str],
    include_thread: bool,
    allows: Sequence[str],
    defaults: Sequence[str],
) -> None:
    if full and json_view:
        raise click.UsageError("--full and --json cannot be combined")
    if pointer is None and focus not in {"model_call", "model_request"}:
        raise click.UsageError(
            "a Pointer is required unless --focus is model_call or model_request"
        )
    if pointer is not None and focus is not None and pointer.field_ref:
        raise click.UsageError("--focus requires a complete record Pointer")
    try:
        bindings = resolve_binding_overrides({}, defaults)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    if pointer is not None and (
        input_source is not None
        or arguments
        or include_thread
        or allows
        or "runnable" in bindings
    ):
        raise click.UsageError(
            "--input, --arg, --thread, --allow, and --default runnable "
            "require prospective model focus"
        )
    if pointer is not None and "model" in bindings and focus != "model_request":
        raise click.UsageError(
            "--default model applies only to model_request focus for stored records"
        )


async def _inspect_value(
    ctx: typer.Context,
    pointer: Pointer | None,
    *,
    focus: str | None,
    resources: ExecutionResources | None,
    input_source: str | None,
    arguments: tuple[str, ...],
    include_thread: bool,
    allows: tuple[str, ...],
    defaults: tuple[str, ...],
) -> object:
    if pointer is not None:
        execution = _require_execution(resources)
        selected = execution.store.resolve_pointer(pointer)
        if focus is None:
            return selected
        focused = _focus_record(execution, selected, focus=focus)
        if focus != "model_request":
            return focused
        setup = await _inspect_setup(ctx, allows=(), defaults=defaults)
        return build_model_request(
            setup,
            model_id=_request_model_id(setup),
            call=cast(ModelCall, focused),
        )

    setup = await _inspect_setup(ctx, allows=allows, defaults=defaults)
    state = await StateWatcher(context_layout(ctx)).refresh()
    thread_id, history = _preview_history(resources, include=include_thread)
    call = _prospective_model_call(
        setup,
        state,
        thread_id=thread_id,
        history=history,
        input_source=input_source,
        arguments=arguments,
    )
    if focus == "model_request":
        return build_model_request(
            setup,
            model_id=_request_model_id(setup),
            call=call,
        )
    return call


def _focus_record(
    resources: ExecutionResources,
    selected: object,
    *,
    focus: str,
) -> ModelCall | ToolCall:
    if not isinstance(selected, StepRecord):
        raise click.ClickException(f"{focus} focus requires a StepRecord")
    if focus in {"model_call", "model_request"}:
        if selected.kind != "model":
            raise click.ClickException(f"step is not a model call: {selected.path}")
        return resources.store.rebuild_model_call(selected)
    if selected.kind != "tool" or not isinstance(selected.given, ToolStepGiven):
        raise click.ClickException(f"step is not a tool call: {selected.path}")
    return selected.given.call


async def _inspect_setup(
    ctx: typer.Context,
    *,
    allows: Sequence[str],
    defaults: Sequence[str],
) -> AgentSetup:
    layout = context_layout(ctx)
    environ = load_runtime_environ(layout, base_environ=os.environ)
    return await SetupWatcher(
        layout,
        model_catalog=context_model_catalog(ctx),
        ceiling_overrides=resolve_ceiling_overrides(environ, allows),
        binding_overrides=resolve_binding_overrides(environ, defaults),
    ).refresh()


def _prospective_model_call(
    setup: AgentSetup,
    state: AgentState,
    *,
    thread_id: str,
    history: Sequence[Message],
    input_source: str | None,
    arguments: Sequence[str],
) -> ModelCall:
    runnable = setup.bindings.runnable
    if runnable is None:
        raise click.ClickException(
            "prospective model focus requires --default runnable=agic:NAME"
        )
    raw_input = _preview_input(input_source, arguments)
    spec = resolve_spec(
        (),
        raw_input,
        setup=setup,
        state=state,
        thread=thread_id,
        default_runnable=runnable,
        include=lambda reference: resolve_file_include(reference, base=Path.cwd()),
    )
    return prepare_model_call(
        spec,
        run_id=_PREVIEW_RUN_ID,
        history=history,
    )


def _preview_input(
    input_source: str | None,
    arguments: Sequence[str],
) -> RunnableInputRaw:
    primary = sys.stdin.read() if input_source == "-" else input_source
    named: list[tuple[str, str]] = []
    for item in arguments:
        name, separator, value = item.partition("=")
        if not separator or not name or not value:
            raise click.UsageError("--arg must use NAME=CONTENT")
        named.append((name, value))
    return RunnableInputRaw(primary=primary, named=tuple(named))


def _preview_history(
    resources: ExecutionResources | None,
    *,
    include: bool,
) -> tuple[str, tuple[Message, ...]]:
    if not include:
        return _PREVIEW_THREAD_ID, ()
    store = _require_execution(resources).store
    threads = RunHistory(store).list_threads(limit=2)
    if not threads:
        raise click.ClickException("--thread requires one current thread")
    if len(threads) > 1:
        raise click.ClickException("--thread is ambiguous: multiple threads exist")
    thread_id = threads[0].id
    return (
        thread_id,
        tuple(
            store.recent_conversation_messages(
                thread_id=thread_id,
                limit=_MODEL_HISTORY_LIMIT,
            )
        ),
    )


def _request_model_id(setup: AgentSetup) -> str:
    model_id = setup.bindings.model
    if model_id is None:
        raise click.ClickException(
            "model_request focus requires --default model=PROVIDER/MODEL_ID"
        )
    return model_id


def _require_execution(
    resources: ExecutionResources | None,
) -> ExecutionResources:
    if resources is None:
        raise RuntimeError("execution resources were not opened")
    return resources


def _focused_data(value: object) -> object:
    if isinstance(value, ThreadRecord | ControlRecord | RunRecord | StepRecord):
        return record_to_data(value)
    if isinstance(value, ModelCall):
        return model_call_to_data(value)
    if isinstance(value, ModelRequest):
        return value.body
    if isinstance(value, ToolCall):
        return _TOOL_CALL_ADAPTER.dump_python(value, mode="json")
    return value


def _render_inspected(
    value: object,
    *,
    pointer: Pointer | None,
    full: bool,
) -> None:
    if isinstance(value, ThreadRecord | ControlRecord | RunRecord | StepRecord):
        _render_record(value, pointer=pointer)
        return
    if isinstance(value, ModelCall):
        _render_model_call(value, full=full)
        return
    if isinstance(value, ModelRequest):
        _section(f"model request · {value.model.ref}")
        _render_json(value.body, full=full)
        return
    if isinstance(value, ToolCall):
        _render_tool_call(value, full=full)
        return
    _section(str(pointer) if pointer is not None else "value")
    if isinstance(value, str):
        typer.echo(_bounded_text(value, full=full))
    elif value is None or isinstance(value, bool | int | float):
        typer.echo(json.dumps(value, ensure_ascii=False))
    else:
        _render_json(value, full=full)


def _render_record(record: Record, *, pointer: Pointer | None) -> None:
    title = type(record).__name__
    _section(f"{title} · {pointer or '-'}")
    rows = [
        (field, _human_cell(value)) for field, value in record_to_data(record).items()
    ]
    echo_table(("FIELD", "VALUE"), rows)


def _render_model_call(call: ModelCall, *, full: bool) -> None:
    data = model_call_to_data(call)
    _section("model call")
    _section("instructions")
    typer.echo(_bounded_text(str(data.get("instructions") or ""), full=full))
    for index, raw_message in enumerate(cast(list[object], data["messages"])):
        message = cast(Mapping[str, object], raw_message)
        _section(f"message {index} · {message.get('role', '-')}")
        for part in cast(list[object], message.get("parts", [])):
            part_data = (
                cast(Mapping[str, object], part) if isinstance(part, Mapping) else None
            )
            if part_data is not None and part_data.get("type") == "text":
                typer.echo(_bounded_text(str(part_data.get("text") or ""), full=full))
            else:
                _render_json(part, full=full)
    tools = cast(list[object], data["tools"])
    if tools:
        _section("tools")
        for tool in tools:
            _render_json(tool, full=full)
    if data.get("state") is not None:
        _section("state")
        _render_json(data["state"], full=full)


def _render_tool_call(call: ToolCall, *, full: bool) -> None:
    data = cast(
        Mapping[str, object],
        _TOOL_CALL_ADAPTER.dump_python(call, mode="json"),
    )
    _section(f"tool call · {data.get('name', '-')}")
    rows = [
        (field, _human_cell(value)) for field, value in data.items() if field != "input"
    ]
    echo_table(("FIELD", "VALUE"), rows)
    _section("input")
    _render_json(data.get("input"), full=full)


def _render_json(value: object, *, full: bool) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    typer.echo(_bounded_text(rendered, full=full))


def _bounded_text(value: str, *, full: bool) -> str:
    if full or len(value) <= _HUMAN_TEXT_LIMIT:
        return value
    omitted = len(value) - _HUMAN_TEXT_LIMIT
    head = (_HUMAN_TEXT_LIMIT * 2) // 3
    tail = _HUMAN_TEXT_LIMIT - head
    return (
        f"{value[:head]}\n\n... [{omitted} characters omitted; use --full] ...\n\n"
        f"{value[-tail:]}"
    )


def _human_cell(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return _truncate(value, width=100)
    if isinstance(value, bool | int | float):
        return json.dumps(value, ensure_ascii=False)
    return _truncate(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        width=100,
    )


def _active_run_id(history: RunHistory, target: str) -> str:
    if target.startswith("run_"):
        run = history.get_run(target)
        if run is None:
            raise click.ClickException(f"run not found: {target}")
        if run.status != "running":
            raise click.ClickException(f"run is not running: {target}")
        return run.id
    thread = history.get_thread(target, run_limit=0)
    if thread is None:
        raise click.ClickException(f"thread not found: {target}")
    if thread.active_run is None:
        raise click.ClickException(f"thread has no active run: {target}")
    return thread.active_run.id


def _anchor(history: RunHistory, target: str) -> tuple[str, str]:
    if target.startswith("run_"):
        run = history.get_run(target)
        if run is None:
            raise click.ClickException(f"run not found: {target}")
        return run.thread_id, run.id
    thread = history.get_thread(target, run_limit=0)
    if thread is None:
        raise click.ClickException(f"thread not found: {target}")
    if thread.latest_run is None:
        raise click.ClickException(f"thread has no runs: {target}")
    return thread.id, thread.latest_run.id


def _open_chat(ctx: typer.Context, thread_id: str) -> None:
    from .chat import chat_command

    chat_command(ctx, thread=thread_id)


async def _restart_run(
    resources: ExecutionResources,
    *,
    layout: AgentLayout,
    kind: Literal["retry", "rerun"],
    source: str,
    anchor: StepPath | None,
    allow_options: list[str] | None,
    default_options: list[str] | None,
    limit_options: list[str] | None,
    show_progress: bool,
    model_catalog: Path | None = None,
) -> RunRecord:
    environ = load_runtime_environ(layout, base_environ=os.environ)
    cli_bindings = resolve_binding_overrides({}, default_options)
    if "runnable" in cli_bindings:
        raise ValueError("--default runnable does not apply to a persisted source run")
    binding_overrides = {
        **resolve_binding_overrides(environ),
        **cli_bindings,
    }
    setup = await SetupWatcher(
        layout,
        model_catalog=model_catalog,
        ceiling_overrides=resolve_ceiling_overrides(environ, allow_options or ()),
        binding_overrides=binding_overrides,
        limit_overrides=resolve_limit_overrides(environ, limit_options or ()),
    ).refresh()
    state = await StateWatcher(layout).refresh()
    executor = RunExecutor(resources.store, resources.ids)
    run_id = source if kind == "retry" else resources.ids.issue_run()
    tracer = (
        ScriptRunPresenter(
            run_id=run_id,
            operation=kind,
            max_width=resolve_progress_max_width(environ),
        )
        if show_progress
        else None
    )
    try:
        handle = (
            executor.retry(
                source,
                setup=setup,
                state=state,
                anchor=anchor,
                model=setup.bindings.model,
                tracer=tracer,
            )
            if kind == "retry"
            else executor.rerun(
                source,
                setup=setup,
                state=state,
                model=setup.bindings.model,
                run_id=run_id,
                tracer=tracer,
            )
        )
        return await handle
    finally:
        try:
            await executor.shutdown()
        finally:
            if tracer is not None:
                tracer.close()


def _retry_anchor(run_id: str, value: str | None) -> StepPath | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise click.ClickException("--anchor requires a step path")
    if text.startswith("run_"):
        return StepPath.parse(text)
    return StepPath.from_local(run_id, text)


def _run_status(value: str | None) -> RunStatus | None:
    if value is None:
        return None
    if value not in {"pending", "running", "succeeded", "failed", "canceled"}:
        raise click.ClickException(f"unknown run status: {value}")
    return cast(RunStatus, value)


def _display_status(value: object) -> str:
    return str(value or "")


def _truncate(value: object, *, width: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return f"{text[: width - 3].rstrip()}..."


def _section(label: str) -> None:
    click.secho(f"# {label}", dim=True)
