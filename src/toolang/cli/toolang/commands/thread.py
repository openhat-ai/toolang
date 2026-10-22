"""Local thread and run control commands."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar
from uuid import uuid4

import typer

from toolang.cli.common.agent_argument import AgentArgument
from typer._click.exceptions import ClickException

from toolang.cli.common.parameters import (
    AllowOptions,
    LimitOptions,
    TextType,
)
from toolang.cli.common.terminal_session import prepare_terminal
from toolang.cli.common.server_lease import ServerLeaseLostError

from toolang.base.errors import ToolangError
from toolang.base.model_settings import parse_model_body
from toolang.base.types.message import Message
from toolang.base.types.model import ModelOverride
from toolang.cli.common.policy import (
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.common.layout import AgentLayout
from toolang.execution.client import RunClient, RunHandle
from toolang.execution.remote import RemoteRunClient, RemoteRunClientError
from toolang.execution.inspection.history import RunHistory
from toolang.execution.replay import replay_request_id
from toolang.execution.schemas import (
    RerunRequest,
    RunDetail,
    ThreadInfo,
)
from toolang.execution.types import (
    RunCommand,
)
from toolang.up.types import AgentServerRef
from toolang.up.config import placement_policy

from ...common.context import (
    cli_context,
    ModelCatalogOption,
    context_layout,
    select_agent_instance,
    load_runtime_environ,
    resolve_model_catalog_option,
    user_call,
)
from ...common.execution import open_execution
from ...common.agent_server import (
    AgentServerAcquisitionError,
    DEVELOPMENT_WHEEL_HELP,
    acquire_agent_server,
)
from ...common.execution_progress.config import resolve_progress_max_width
from ...common.run_client import acquire_run_client
from ...common.terminal_surfaces import resolve_terminal_surfaces


def steer_command(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("home", requires_live=True)],
    run: Annotated[
        str,
        typer.Argument(
            metavar="RUN",
            click_type=TextType(),
            help="Run ID or thread ID; a thread selects its active run",
        ),
    ],
    message: Annotated[
        str,
        typer.Argument(
            metavar="MESSAGE",
            click_type=TextType(),
            help="Instruction to steer the run",
        ),
    ],
) -> None:
    """Persist one next-step steer control."""

    with open_execution(ctx, required=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        run_id = _active_run_id(RunHistory(resources.store), run)
        record = resources.store.get_run(run_id=run_id)
        expected = record.control if record is not None else None
    _remote_action(
        context_layout(ctx),
        lambda client: client.steer(
            run_id,
            Message.user(message),
            **({"expected_control": expected} if expected is not None else {}),
        ),
        active_only=True,
        expected_launch_id=cli_context(ctx).launch_id,
    )
    typer.echo(f"steered {run_id}")


def cancel_command(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("home", requires_live=True)],
    run: Annotated[
        str,
        typer.Argument(
            metavar="RUN",
            click_type=TextType(),
            help="Run ID or thread ID; a thread selects its active run",
        ),
    ],
) -> None:
    """Persist one immediate cancel control."""

    with open_execution(ctx, required=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        run_id = _active_run_id(RunHistory(resources.store), run)
        record = resources.store.get_run(run_id=run_id)
        expected = record.control if record is not None else None
    _remote_action(
        context_layout(ctx),
        lambda client: client.cancel(
            run_id, **({"expected_control": expected} if expected is not None else {})
        ),
        active_only=True,
        expected_launch_id=cli_context(ctx).launch_id,
    )
    typer.echo(f"canceled {run_id}")


def rerun_command(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("home")],
    run: Annotated[
        str,
        typer.Argument(
            metavar="RUN",
            click_type=TextType(),
            help="Run ID or thread ID; a thread selects its latest visible root run",
        ),
    ],
    model_catalog: ModelCatalogOption = None,
    sandbox: Annotated[
        str | None,
        typer.Option(
            "--sandbox",
            metavar="SANDBOX",
            help="Execute the new run in this sandbox",
        ),
    ] = None,
    allows: AllowOptions = None,
    limit: LimitOptions = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Override the model; see 'too help model'",
            metavar="MODEL",
        ),
    ] = None,
    dev: Annotated[
        Path | None,
        typer.Option("--dev", metavar="[PATH]", help=DEVELOPMENT_WHEEL_HELP),
    ] = None,
) -> None:
    """Start a new root run from one terminal source invocation."""

    layout = select_agent_instance(ctx)
    show_progress = sys.stderr.isatty()
    model_override = user_call(_rerun_model_override, model_body=model)
    commands = user_call(_restart_commands, allow_options=allows, limit_options=limit)
    selected_catalog = resolve_model_catalog_option(model_catalog)
    with open_execution(ctx, required=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        _thread_id, source = _anchor(RunHistory(resources.store), run)
    result = _run_rerun(
        layout=layout,
        source=source,
        sandbox=sandbox,
        dev=dev,
        commands=commands,
        model_override=model_override,
        show_progress=show_progress,
        model_catalog=selected_catalog,
        expected_launch_id=cli_context(ctx).launch_id,
    )
    status = _display_status(result.status)
    if not show_progress:
        typer.echo(f"reran {source} as {result.id}: {status}")
    if result.status != "succeeded":
        raise typer.Exit(1)


def rewind_command(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("home")],
    point: Annotated[
        str,
        typer.Argument(
            metavar="RUN",
            click_type=TextType(),
            help="Run ID to rewind before; a thread ID selects its latest root run",
        ),
    ],
    chat: Annotated[
        bool, typer.Option("--chat", help="Open chat on the rewound thread")
    ] = False,
) -> None:
    """Rewind one idle thread before a terminal anchor run."""

    with open_execution(ctx, required=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        history = RunHistory(resources.store)
        thread_id, run_id = _anchor(history, point)
    handled, _edited = _offline_edit(
        ctx, lambda manager: manager.rewind(thread_id=thread_id, run_id=run_id)
    )
    if handled:
        typer.echo(f"rewound {thread_id} before {run_id}")
        if chat:
            _open_chat(ctx, thread_id)
        return
    shared_chat = (
        chat and placement_policy(context_layout(ctx).placement).reuse_executor
    )

    def continue_chat(_result: object, server: AgentServerRef) -> None:
        typer.echo(f"rewound {thread_id} before {run_id}")
        _open_chat(ctx, thread_id, server)

    _remote_action(
        context_layout(ctx),
        lambda client: client.rewind_thread(thread_id, run_id=run_id),
        on_result=continue_chat if shared_chat else None,
        expected_launch_id=cli_context(ctx).launch_id,
    )
    if not shared_chat:
        typer.echo(f"rewound {thread_id} before {run_id}")
    if chat and not shared_chat:
        _open_chat(ctx, thread_id)


def fork_command(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("home")],
    point: Annotated[
        str,
        typer.Argument(
            metavar="RUN",
            click_type=TextType(),
            help="Run ID to fork through; a thread ID selects its latest root run",
        ),
    ],
    chat: Annotated[
        bool, typer.Option("--chat", help="Open chat on the forked thread")
    ] = False,
) -> None:
    """Fork one thread through a terminal anchor run."""

    with open_execution(ctx, required=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        history = RunHistory(resources.store)
        thread_id, run_id = _anchor(history, point)
    handled, edited = _offline_edit(
        ctx, lambda manager: manager.fork(thread_id=thread_id, run_id=run_id)
    )
    if handled:
        typer.echo(f"forked {edited} through {run_id}")
        if chat:
            _open_chat(ctx, str(edited))
        return
    shared_chat = (
        chat and placement_policy(context_layout(ctx).placement).reuse_executor
    )

    def continue_chat(result: ThreadInfo, server: AgentServerRef) -> None:
        typer.echo(f"forked {result.id} through {run_id}")
        _open_chat(ctx, result.id, server)

    forked = _remote_action(
        context_layout(ctx),
        lambda client: client.fork_thread(thread_id, run_id=run_id),
        on_result=continue_chat if shared_chat else None,
        expected_launch_id=cli_context(ctx).launch_id,
    )
    forked_id = forked.id
    if not shared_chat:
        typer.echo(f"forked {forked_id} through {run_id}")
    if chat and not shared_chat:
        _open_chat(ctx, forked_id)


_Result = TypeVar("_Result")


def _remote_action(
    layout: AgentLayout,
    action: Callable[[RemoteRunClient], Awaitable[_Result]],
    *,
    active_only: bool = True,
    expected_launch_id: str | None = None,
    on_result: Callable[[_Result, AgentServerRef], None] | None = None,
) -> _Result:
    async def execute(server: AgentServerRef) -> _Result:
        async with acquire_run_client(server) as client:
            return await action(client)

    try:
        with acquire_agent_server(
            layout,
            sandbox=None,
            attach_only=active_only,
            expected_launch_id=expected_launch_id,
        ) as server:
            if server is None:
                raise AgentServerAcquisitionError("agent acquisition requires a server")
            if active_only and server.launch_id is None:
                raise AgentServerAcquisitionError(
                    "server does not support guarded controls; stop and restart it"
                )
            result = asyncio.run(execute(server))
            if on_result is not None:
                on_result(result, server)
            return result
    except (OSError, ToolangError, ValueError, RuntimeError) as exc:
        raise ClickException(str(exc)) from exc


def _active_run_id(history: RunHistory, target: str) -> str:
    if target.startswith("run_"):
        run = history.get_run(target)
        if run is None:
            raise ClickException(f"run not found: {target}")
        if run.status not in {"pending", "running"}:
            raise ClickException(f"run is not active: {target}")
        return run.id
    thread = history.get_thread(target, run_limit=0)
    if thread is None:
        raise ClickException(f"thread not found: {target}")
    if thread.active_run is None:
        raise ClickException(f"thread has no active run: {target}")
    return thread.active_run.id


def _anchor(history: RunHistory, target: str) -> tuple[str, str]:
    if target.startswith("run_"):
        run = history.get_run(target)
        if run is None:
            raise ClickException(f"run not found: {target}")
        return run.thread_id, run.id
    thread = history.get_thread(target, run_limit=0)
    if thread is None:
        raise ClickException(f"thread not found: {target}")
    if thread.latest_run is None:
        raise ClickException(f"thread has no runs: {target}")
    return thread.id, thread.latest_run.id


def _open_chat(
    ctx: typer.Context, thread_id: str, server: AgentServerRef | None = None
) -> None:
    from .chat import chat_command

    if server is None:
        chat_command(ctx, agent=context_layout(ctx).name, thread=thread_id)
    else:
        from .chat.main import chat_command as continue_chat

        continue_chat(ctx, thread=thread_id, server=server)


def _run_rerun(
    *,
    layout: AgentLayout,
    source: str,
    sandbox: str | None,
    dev: Path | None,
    commands: tuple[RunCommand, ...],
    model_override: ModelOverride | None,
    show_progress: bool,
    model_catalog: Path | None = None,
    expected_launch_id: str | None = None,
) -> RunDetail:
    try:
        with (
            prepare_terminal(
                "rerun",
                layout=layout,
                label=f"rerun {source}",
                eligible=show_progress,
                stream=sys.stderr,
            ) as terminal,
            acquire_agent_server(
                layout,
                sandbox=sandbox,
                dev=dev,
                model_catalog=model_catalog,
                show_progress=show_progress,
                bound=True,
                expected_launch_id=expected_launch_id,
            ) as server,
        ):
            if server is None:
                raise AgentServerAcquisitionError("agent acquisition requires a server")
            return asyncio.run(
                _execute_rerun(
                    layout=layout,
                    server=server,
                    source=source,
                    commands=commands,
                    model_override=model_override,
                    show_progress=show_progress,
                    on_accept=terminal.set_run,
                )
            )
    except AgentServerAcquisitionError as exc:
        raise ClickException(str(exc)) from exc
    except (OSError, ToolangError, ValueError, RuntimeError) as exc:
        raise ClickException(str(exc)) from exc


def _restart_commands(
    *,
    allow_options: list[str] | None,
    limit_options: list[str] | None,
) -> tuple[RunCommand, ...]:
    ceilings = resolve_ceiling_overrides({}, allow_options or ())
    limits = resolve_limit_overrides({}, limit_options or ())
    return (
        *(RunCommand("allow", field, value) for field, value in ceilings.items()),
        *(RunCommand("limit", field, value) for field, value in limits.items()),
    )


def _rerun_model_override(
    *,
    model_body: str | None,
) -> ModelOverride | None:
    resolved = parse_model_body(model_body) if model_body is not None else None
    if resolved is not None and resolved.identity == "unset":
        raise ValueError("rerun --model does not accept unset")
    return resolved


async def _execute_rerun(
    *,
    layout: AgentLayout,
    server: AgentServerRef,
    source: str,
    commands: tuple[RunCommand, ...],
    model_override: ModelOverride | None,
    show_progress: bool,
    on_accept: Callable[[str], None] | None = None,
) -> RunDetail:
    from ...common.script_progress import ScriptRunPresenter

    environ = load_runtime_environ(layout, base_environ=os.environ)
    tracer = (
        ScriptRunPresenter(
            run_id=None,
            operation="rerun",
            max_width=resolve_progress_max_width(environ),
            surfaces=resolve_terminal_surfaces(
                environment=environ, output_stream=sys.stderr
            ),
        )
        if show_progress
        else None
    )
    try:
        async with acquire_run_client(server) as client:
            request_id = f"term_{uuid4().hex}"
            request_id = replay_request_id(layout.run_store, source) or request_id
            handle = await client.rerun(
                RerunRequest(
                    source=source,
                    commands=commands,
                    request_id=request_id,
                    model_override=model_override,
                ),
                tracer=tracer,
            )
            if on_accept is not None:
                on_accept(handle.run_id)
            try:
                return await handle.wait()
            except (asyncio.CancelledError, KeyboardInterrupt):
                if server.check_retention is not None:
                    try:
                        server.check_retention()
                    except ServerLeaseLostError as exc:
                        raise ServerLeaseLostError(
                            f"{exc}; inspect accepted run {handle.run_id}"
                        ) from exc
                await _cancel_restart(client, handle)
                raise
            except RemoteRunClientError as exc:
                raise RemoteRunClientError(
                    f"{exc}; inspect accepted run {handle.run_id}",
                    status_code=exc.status_code,
                    detail=exc.detail,
                ) from exc
    finally:
        if tracer is not None:
            tracer.close()


async def _cancel_restart(
    client: RunClient,
    handle: RunHandle,
) -> None:
    try:
        await client.cancel(
            handle.run_id,
            request_id=f"term_{uuid4().hex}",
            reason="rerun interrupted",
        )
    except (OSError, ValueError, RuntimeError):
        return
    try:
        await asyncio.wait_for(asyncio.shield(handle.wait()), timeout=5)
    except (TimeoutError, OSError, ValueError, RuntimeError):
        pass


def _display_status(value: object) -> str:
    return str(value or "")


def _offline_edit(ctx: typer.Context, edit):
    from toolang.up import process, sandbox as runtime
    from toolang.up.instance import read_instance, process_alive
    from toolang.execution.store import RunStore
    from toolang.execution.threads import ThreadManager
    from toolang.common.ids import IdIssuer

    layout = context_layout(ctx)
    status = process.AgentProcess(layout).status(ui_base_url="", check_health=False)
    if status is not None and status.status == "running":
        return False, None
    with runtime.management_lock(layout):
        entry = read_instance(layout.runtime_status)
        if entry is None or entry["workload"].get("status") not in {
            "stopped",
            "failed",
        }:
            raise ClickException("offline edits require a conclusively stopped owner")
        if (
            cli_context(ctx).launch_id is not None
            and entry["launch_id"] != cli_context(ctx).launch_id
        ):
            raise ClickException("selected execution owner was replaced")
        report = entry["workload"]
        if (
            report.get("pid") is not None
            and process_alive(report.get("pid"), report.get("process_created"))
            is not False
        ):
            raise ClickException("offline owner liveness is unknown")
        owner = process.AgentProcess(layout).reference
        if (
            owner is not None
            and asyncio.run(
                runtime.load_state_sandbox(layout, owner).running(owner.ref)
            )
            is not False
        ):
            raise ClickException("sandbox owner is not conclusively stopped")
        store = RunStore(layout.run_store)
        try:
            manager = ThreadManager(
                store, IdIssuer(layout.run_store, agent_name=layout.name)
            )
            return True, edit(manager)
        finally:
            store.close()
