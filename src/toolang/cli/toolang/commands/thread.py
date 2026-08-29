"""Local thread and run inspection and control commands."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Annotated, Literal, cast
from uuid import uuid4

import click
import typer
from rich import box
from rich.cells import cell_len
from rich.console import Console, RenderableType
from rich.table import Table
from rich.text import Text

from toolang.base.errors import ToolangError
from toolang.base.types.message import Message
from toolang.cli.common.human_values import (
    human_scalar_text,
    human_value_renderable,
)
from toolang.cli.common.policy import (
    resolve_binding_overrides,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.common.layout import AgentLayout
from toolang.execution.client import RunClient, RunHandle
from toolang.execution.executor import RunExecutor
from toolang.execution.history import RunHistory
from toolang.execution.records import PreparationControlPayload
from toolang.execution.schemas import (
    RecordSelection,
    RerunRequest,
    RetryRequest,
    RunDetail,
)
from toolang.execution.threads import ThreadManager
from toolang.execution.store import RunStore
from toolang.execution.types import (
    Local,
    Pointer,
    RunOverride,
    RunStatus,
    StepPath,
    TypedPointer,
    local_to_protocol_data,
    validate_runtime_value,
)

from ...common.context import (
    context_layout,
    context_model_catalog,
    load_runtime_environ,
    user_call,
)
from ...common.execution import ExecutionResources, open_execution
from ...common.execution_runtime import (
    DEVELOPMENT_WHEEL_HELP,
    ExecutionRuntime,
    ExecutionRuntimeError,
    open_execution_runtime,
)
from ...common.execution_progress.config import resolve_progress_max_width
from ...common.output import echo_table
from ...common.run_client import open_run_client
from ...common.script_progress import ScriptRunPresenter


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
    pointer: Annotated[
        str, typer.Argument(help="Historical record or field Pointer to inspect.")
    ],
    human: Annotated[
        bool, typer.Option("--human", help="Render a human-readable value.")
    ] = False,
    json_view: Annotated[
        bool, typer.Option("--json", help="Render exact canonical JSON.")
    ] = False,
) -> None:
    """Inspect one historical execution record or field."""

    if human and json_view:
        raise click.UsageError("--human and --json are mutually exclusive")
    try:
        parsed = Pointer(pointer)
    except (TypeError, ValueError) as exc:
        raise click.UsageError(str(exc)) from exc
    with open_execution(ctx, required=True) as resources:
        if resources is None:  # pragma: no cover - required=True guarantees this
            raise RuntimeError("execution resources were not opened")
        try:
            selected = resources.store.select_pointer(parsed)
            if json_view:
                typer.echo(json.dumps(selected.value, ensure_ascii=False, indent=2))
            else:
                _render_pointer(resources.store, selected)
        except (TypeError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class _HumanValue:
    data: object
    runtime: object
    render_type: str
    resolved: bool


def _render_pointer(store: RunStore, selected: RecordSelection) -> None:
    console = Console(highlight=False)
    root = _human_value(store, selected)
    browsable = _browse_children(selected, root)
    if browsable:
        _render_human_rows(
            console,
            _human_children(store, selected),
            base=selected.pointer,
        )
    else:
        block = _human_block(root)
        if block is not None:
            console.print(block)
        else:
            console.print(Text(_human_summary(root)), soft_wrap=True)

    console.print()
    relation = "resolves to" if root.resolved else "has type"
    suffix = "; append a FIELD to inspect a child." if browsable else "."
    context = Text(
        f"{selected.pointer} {relation} {_human_type_label(root.render_type)}{suffix}",
        style="dim",
    )
    console.print(context, soft_wrap=True)


def _browse_children(selected: RecordSelection, value: _HumanValue) -> bool:
    if value.resolved or isinstance(selected.runtime, Local):
        return False
    if value.render_type in {"Part", "Part[]"}:
        return False
    return isinstance(selected.value, Mapping | list) and bool(selected.value)


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
) -> None:
    compact: list[tuple[str, str, RenderableType]] = []
    pointer_heading = "FIELD" if base is not None else "POINTER"
    for selected, value in rows:
        pointer = str(selected.pointer)
        if base is not None:
            pointer = pointer.removeprefix(str(base))
        label = f"{pointer}{' →' if value.resolved else ''}"
        rendered = _human_block(value)
        compact.append(
            (
                label,
                _human_type_label(value.render_type),
                rendered if rendered is not None else _human_summary(value),
            )
        )
    _print_human_table(console, compact, pointer_heading=pointer_heading)


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
    """Persist one immediate cancel control."""

    with open_execution(ctx, required=True, writable=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        run_id = _active_run_id(RunHistory(resources.store), run)
        user_call(
            RunExecutor(resources.store, resources.ids).cancel,
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
    dev: Annotated[
        Path | None,
        typer.Option(
            "--dev",
            help=DEVELOPMENT_WHEEL_HELP,
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

    layout = context_layout(ctx)
    with open_execution(ctx, required=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        _thread_id, run_id = _anchor(RunHistory(resources.store), run)
        sandbox = user_call(_retry_sandbox, resources, run_id)
    show_progress = sys.stderr.isatty()
    result = _run_retry_or_rerun(
        layout=layout,
        kind="retry",
        source=run_id,
        anchor=user_call(_retry_anchor, run_id, anchor),
        sandbox=sandbox,
        dev=dev,
        commands=user_call(
            _restart_commands,
            layout,
            allow_options=allows,
            default_options=defaults,
            limit_options=limit,
        ),
        show_progress=show_progress,
        model_catalog=context_model_catalog(ctx),
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
    sandbox: Annotated[
        str | None,
        typer.Option("--sandbox", help="Execute the new run in this sandbox."),
    ] = None,
    dev: Annotated[
        Path | None,
        typer.Option(
            "--dev",
            help=DEVELOPMENT_WHEEL_HELP,
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
        typer.Option("--default", help="Set model=VALUE for the new run."),
    ] = None,
) -> None:
    """Start a new root run from one terminal source invocation."""

    layout = context_layout(ctx)
    with open_execution(ctx, required=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        _thread_id, source = _anchor(RunHistory(resources.store), run)
    show_progress = sys.stderr.isatty()
    result = _run_retry_or_rerun(
        layout=layout,
        kind="rerun",
        source=source,
        anchor=None,
        sandbox=sandbox,
        dev=dev,
        commands=user_call(
            _restart_commands,
            layout,
            allow_options=allows,
            default_options=defaults,
            limit_options=limit,
        ),
        show_progress=show_progress,
        model_catalog=context_model_catalog(ctx),
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


def _run_retry_or_rerun(
    *,
    layout: AgentLayout,
    kind: Literal["retry", "rerun"],
    source: str,
    anchor: StepPath | None,
    sandbox: str | None,
    dev: Path | None,
    commands: tuple[RunOverride, ...],
    show_progress: bool,
    model_catalog: Path | None = None,
) -> RunDetail:
    try:
        with open_execution_runtime(
            layout,
            sandbox=sandbox,
            dev=dev,
            model_catalog=model_catalog,
            show_progress=show_progress,
        ) as runtime:
            return asyncio.run(
                _execute_retry_or_rerun(
                    layout=layout,
                    runtime=runtime,
                    kind=kind,
                    source=source,
                    anchor=anchor,
                    commands=commands,
                    show_progress=show_progress,
                    model_catalog=model_catalog,
                )
            )
    except ExecutionRuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    except (OSError, ToolangError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


def _restart_commands(
    layout: AgentLayout,
    *,
    allow_options: list[str] | None,
    default_options: list[str] | None,
    limit_options: list[str] | None,
) -> tuple[RunOverride, ...]:
    environ = load_runtime_environ(layout, base_environ=os.environ)
    cli_bindings = resolve_binding_overrides({}, default_options)
    if "runnable" in cli_bindings:
        raise ValueError("--default runnable does not apply to a persisted source run")
    binding_overrides = {
        **resolve_binding_overrides(environ),
        **cli_bindings,
    }
    binding_overrides.pop("runnable", None)
    ceilings = resolve_ceiling_overrides(environ, allow_options or ())
    limits = resolve_limit_overrides(environ, limit_options or ())
    return (
        *(RunOverride("allow", field, value) for field, value in ceilings.items()),
        *(
            RunOverride("default", field, value)
            for field, value in binding_overrides.items()
        ),
        *(RunOverride("limit", field, value) for field, value in limits.items()),
    )


async def _execute_retry_or_rerun(
    *,
    layout: AgentLayout,
    runtime: ExecutionRuntime,
    kind: Literal["retry", "rerun"],
    source: str,
    anchor: StepPath | None,
    commands: tuple[RunOverride, ...],
    show_progress: bool,
    model_catalog: Path | None,
) -> RunDetail:
    environ = load_runtime_environ(layout, base_environ=os.environ)
    run_id = source if kind == "retry" else None
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
        async with open_run_client(
            layout,
            runtime,
            model_catalog=model_catalog,
        ) as client:
            request_id = f"term_{uuid4().hex}"
            handle = (
                await client.retry(
                    RetryRequest(
                        source=source,
                        commands=commands,
                        request_id=request_id,
                        anchor=anchor,
                    ),
                    tracer=tracer,
                )
                if kind == "retry"
                else await client.rerun(
                    RerunRequest(
                        source=source,
                        commands=commands,
                        request_id=request_id,
                    ),
                    tracer=tracer,
                )
            )
            try:
                return await handle.wait()
            except BaseException:
                await _cancel_restart(client, handle, operation=kind)
                raise
    finally:
        if tracer is not None:
            tracer.close()


def _retry_sandbox(
    resources: ExecutionResources,
    run_id: str,
) -> str:
    run = resources.store.get_run(run_id=run_id)
    if run is None or run.parent is not None:
        raise ValueError(f"root run not found: {run_id}")
    control = resources.store.get_run_control(
        run_id=run.control.target,
        index=run.control.index,
    )
    if control is None or not isinstance(control.payload, PreparationControlPayload):
        raise ValueError(f"run preparation not found: {run_id}")
    if control.payload.sandbox is None:
        raise ValueError(f"retry sandbox is unknown for run {run_id}; use rerun")
    return control.payload.sandbox


async def _cancel_restart(
    client: RunClient,
    handle: RunHandle,
    *,
    operation: str,
) -> None:
    try:
        await client.cancel(
            handle.run_id,
            request_id=f"term_{uuid4().hex}",
            reason=f"{operation} interrupted",
        )
    except (OSError, ValueError, RuntimeError):
        return
    try:
        await asyncio.wait_for(asyncio.shield(handle.wait()), timeout=5)
    except (TimeoutError, OSError, ValueError, RuntimeError):
        pass


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
