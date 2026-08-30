"""Local thread and run inspection and control commands."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from typing import Annotated, Literal, cast
from uuid import uuid4

import click
import typer

from toolang.base.errors import ToolangError
from toolang.base.types.message import Message
from toolang.cli.common.policy import (
    resolve_binding_overrides,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.common.layout import AgentLayout
from toolang.execution.client import RunClient, RunHandle
from toolang.execution.executor import RunExecutor
from toolang.execution.history import RunHistory
from toolang.execution.records import (
    PreparationControlPayload,
)
from toolang.execution.schemas import (
    RerunRequest,
    RetryRequest,
    RunDetail,
)
from toolang.execution.threads import ThreadManager
from toolang.execution.types import (
    RunOverride,
    RunStatus,
    StepPath,
)
from toolang.up.types import AgentServerRef

from ...common.context import (
    ModelCatalogOption,
    context_layout,
    load_runtime_environ,
    resolve_model_catalog_option,
    user_call,
)
from ...common.execution import ExecutionResources, open_execution
from ...common.agent_server import (
    AgentServerAcquisitionError,
    DEVELOPMENT_WHEEL_HELP,
    acquire_agent_server,
)
from ...common.execution_progress.config import resolve_progress_max_width
from ...common.output import echo_table
from ...common.run_client import acquire_run_client
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
    model_catalog: ModelCatalogOption = None,
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
        model_catalog=resolve_model_catalog_option(model_catalog),
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
    model_catalog: ModelCatalogOption = None,
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
        model_catalog=resolve_model_catalog_option(model_catalog),
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
        with acquire_agent_server(
            layout,
            sandbox=sandbox,
            dev=dev,
            model_catalog=model_catalog,
            show_progress=show_progress,
        ) as server:
            return asyncio.run(
                _execute_retry_or_rerun(
                    layout=layout,
                    server=server,
                    kind=kind,
                    source=source,
                    anchor=anchor,
                    commands=commands,
                    show_progress=show_progress,
                    model_catalog=model_catalog,
                )
            )
    except AgentServerAcquisitionError as exc:
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
    server: AgentServerRef | None,
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
        async with acquire_run_client(
            layout,
            server,
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
