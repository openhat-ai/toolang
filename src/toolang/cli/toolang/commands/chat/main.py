"""Process-local terminal chat commands."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import sys
from typing import cast

import click
import typer

from toolang.base.types.message import TextDelta, TextPart, message_text
from toolang.cli.common.policy import (
    resolve_default_overrides,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.common.errors import ToolangError
from toolang.execution.events import PartDelta, RunBegin, RunEnd, RunEvent, StepEnd
from toolang.execution.history import RunHistory
from toolang.execution.records import execution_error_message
from toolang.execution.policy import merge_run_overrides, parse_setting_override
from toolang.execution.types import (
    AllowField,
    AllowOverride,
    LimitField,
    LimitOverride,
    ModelOverride,
    RunOverride,
    SessionSetting,
    StepPath,
)
from toolang.lang.types import Array
from toolang.cli.common.context import (
    context_layout,
    load_runtime_environ,
    resolve_model_catalog_option,
    ui_base_url,
    user_call,
)
from toolang.cli.common.execution import open_execution
from toolang.cli.common.agent_server import (
    AgentServerAcquisitionError,
    acquire_agent_server,
)
from toolang.cli.common.execution_progress.config import resolve_progress_max_width
from toolang.cli.common.output import shorten_home_path
from . import slashes as chat_slashes
from .base import (
    ChatClient,
    ChatRunState,
    RunBlocked,
    RunRecovered,
    chat_status_label,
    friendly_error as chat_friendly_error,
)
from .history import ChatInputHistoryStore
from .input import (
    QuickCommand,
    is_runnable_input,
    normalize_chat_input,
    parse_chat_input,
)
from .local import LocalChatSession
from .remote import RemoteChatError, RemoteChatSession
from .tui import ChatTuiApp


def chat_command(
    ctx: typer.Context,
    thread: str | None = None,
    model_catalog: Path | None = None,
    allows: list[str] | None = None,
    defaults: list[str] | None = None,
    sandbox: str | None = None,
    dev: Path | None = None,
    limits: list[str] | None = None,
) -> None:
    thread_id = _target_thread_id(ctx, thread) if thread is not None else None
    _chat_interactive(
        ctx,
        thread_id=thread_id,
        model_catalog=model_catalog,
        sandbox=sandbox,
        dev=dev,
        allow_options=allows,
        default_options=defaults,
        limit_options=limits,
    )


def _chat_interactive(
    ctx: typer.Context,
    *,
    thread_id: str | None,
    model_catalog: Path | None = None,
    sandbox: str | None = None,
    dev: Path | None = None,
    allow_options: list[str] | None = None,
    default_options: list[str] | None = None,
    limit_options: list[str] | None = None,
) -> None:
    with _chat_runtime(
        ctx,
        model_catalog=model_catalog,
        sandbox=sandbox,
        dev=dev,
    ) as client:
        setting = client.initial_setting()
        initial_update = _chat_session_override(
            allow_options=allow_options,
            default_options=default_options,
            limit_options=limit_options,
        )
        if not initial_update.empty:
            setting = client.apply_setting(setting, initial_update)
        cli_defaults = user_call(resolve_default_overrides, {}, default_options)
        if "runnable" in cli_defaults and cli_defaults["runnable"] is None:
            setting = replace(setting, runnable=None)
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            _chat_interactive_scripted_local(
                client=client,
                thread_id=thread_id,
                setting=setting,
            )
            return
        _chat_interactive_prompt_toolkit(
            ctx,
            thread_id=thread_id,
            setting=setting,
            client=client,
        )


@contextmanager
def _chat_runtime(
    ctx: typer.Context,
    *,
    model_catalog: Path | None = None,
    sandbox: str | None,
    dev: Path | None = None,
) -> Iterator[ChatClient]:
    """Own one local, attached, or temporary-remote Chat session."""

    layout = context_layout(ctx)
    try:
        server_context = acquire_agent_server(
            layout,
            sandbox=sandbox,
            dev=dev,
            model_catalog=resolve_model_catalog_option(model_catalog),
            ui_base_url=ui_base_url(),
        )
        with server_context as server:
            if server is not None:
                remote: RemoteChatSession | None = None
                try:
                    remote = RemoteChatSession(
                        server.endpoint,
                        expected_sandbox=server.sandbox,
                    )
                except (RemoteChatError, ValueError) as exc:
                    if remote is not None:
                        remote.close()
                    raise click.ClickException(str(exc)) from exc
                try:
                    yield remote
                finally:
                    remote.close()
                return

            environ = load_runtime_environ(layout, base_environ=os.environ)
            local = LocalChatSession(
                layout,
                sandbox="host",
                **(
                    {"model_catalog": model_catalog}
                    if model_catalog is not None
                    else {}
                ),
                ceiling_overrides=user_call(
                    resolve_ceiling_overrides,
                    environ,
                ),
                default_overrides=user_call(
                    resolve_default_overrides,
                    environ,
                ),
                limit_overrides=user_call(
                    resolve_limit_overrides,
                    environ,
                ),
            )
            try:
                yield local
            finally:
                local.close()
    except AgentServerAcquisitionError as exc:
        raise click.ClickException(str(exc)) from exc


def _chat_session_override(
    *,
    allow_options: list[str] | None,
    default_options: list[str] | None,
    limit_options: list[str] | None,
) -> RunOverride:
    ceilings = user_call(resolve_ceiling_overrides, {}, allow_options)
    defaults = user_call(resolve_default_overrides, {}, default_options)
    limits = user_call(resolve_limit_overrides, {}, limit_options)
    updates: list[RunOverride] = []
    if ceilings:
        updates.append(
            RunOverride(
                allow=tuple(
                    AllowOverride(cast(AllowField, field), value)
                    for field, value in ceilings.items()
                ),
            )
        )
    model = defaults.get("model") if "model" in defaults else None
    runnable = defaults.get("runnable") if "runnable" in defaults else None
    if "model" in defaults or "runnable" in defaults:
        updates.append(
            RunOverride(
                model=(
                    ModelOverride(identity=model if model is not None else "none")
                    if "model" in defaults
                    else None
                ),
                runnable=runnable if runnable is not None else None,
            )
        )
    if limits:
        updates.append(
            RunOverride(
                limits=tuple(
                    LimitOverride(cast(LimitField, field), value)
                    for field, value in limits.items()
                )
            )
        )
    return merge_run_overrides(updates)


def _chat_input_history_store(ctx: typer.Context) -> ChatInputHistoryStore | None:
    try:
        layout = context_layout(ctx)
    except (AttributeError, KeyError, TypeError):
        return None
    return ChatInputHistoryStore(layout.runtime / "chat-input-history.jsonl")


def _chat_home_label(ctx: typer.Context) -> str:
    try:
        return shorten_home_path(context_layout(ctx).home)
    except Exception:
        return "agent home"


def _chat_interactive_prompt_toolkit(
    ctx: typer.Context,
    *,
    thread_id: str | None,
    setting: SessionSetting,
    client: ChatClient,
) -> None:
    ChatTuiApp.run(
        thread_id=thread_id,
        setting=setting,
        home=_chat_home_label(ctx),
        input_history=_chat_input_history_store(ctx),
        client=client,
        progress_max_width=user_call(
            resolve_progress_max_width,
            load_runtime_environ(context_layout(ctx), base_environ=os.environ),
        ),
    )


def _chat_interactive_scripted_local(
    *,
    client: ChatClient,
    thread_id: str | None,
    setting: SessionSetting,
) -> None:
    renderer = _ScriptedRunRenderer()

    def ensure_thread_id() -> str:
        nonlocal thread_id
        if thread_id is None:
            thread_id = client.create_thread()
            typer.echo(f"thread {thread_id}")
        return thread_id

    if thread_id is not None:
        typer.echo(f"thread {thread_id}")
    while True:
        try:
            text = input("> ")
        except EOFError:
            return
        except KeyboardInterrupt:
            typer.echo()
            return
        if text.strip() in {"/exit", "/quit"}:
            return
        if not text.strip():
            continue
        source = normalize_chat_input(text)
        try:
            chat_input = parse_chat_input(source)
        except ValueError as exc:
            typer.echo(chat_friendly_error(str(exc)), err=True)
            continue
        if isinstance(chat_input, QuickCommand):
            setting = _chat_handle_scripted_command(
                chat_input,
                setting,
                client=client,
            )
            continue
        if not is_runnable_input(chat_input):
            raise AssertionError("unknown chat input value")
        override, runnable_input = chat_input
        errors: list[str] = []
        renderer.reset()
        try:
            request = client.build_request(
                ensure_thread_id(),
                override,
                runnable_input,
                setting,
            )
        except (click.ClickException, ToolangError, ValueError) as exc:
            detail = exc.message if isinstance(exc, click.ClickException) else str(exc)
            typer.echo(chat_friendly_error(detail), err=True)
            continue
        client.run(request, renderer.render, errors.append, renderer.handle_state)
        failure = errors[-1] if errors else renderer.failure
        if failure:
            typer.echo(chat_friendly_error(failure), err=True)


def _chat_handle_scripted_command(
    chat_input: QuickCommand,
    setting: SessionSetting,
    *,
    client: ChatClient,
) -> SessionSetting:
    command = chat_input.name
    if command in {"help", "?"}:
        for line in chat_slashes._chat_help_lines():
            typer.echo(line)
        return setting
    if command not in {"model", "agic", "flow", "runnable", "allow", "limit"}:
        typer.echo(f"Unknown command: /{command}")
        return setting
    try:
        update = _scripted_setting_update(command, chat_input.tail, client=client)
        setting = client.apply_setting(setting, update)
    except (click.ClickException, ToolangError, ValueError) as exc:
        detail = exc.message if isinstance(exc, click.ClickException) else str(exc)
        typer.echo(chat_friendly_error(detail), err=True)
        return setting
    typer.echo(chat_status_label(setting))
    return setting


def _scripted_setting_update(
    command: str,
    argument: str | None,
    *,
    client: ChatClient,
) -> RunOverride:
    if argument is None:
        raise ValueError(f"/{command} requires a setting body")
    update = parse_setting_override(command, argument)
    if command == "model":
        model = update.model
        if model is not None and model.identity not in {None, "default", "none"}:
            resolved = chat_slashes._chat_resolve_model_command(
                client.list_models(), model.identity
            )
            if resolved is None:
                raise ValueError(
                    f"Model selection is unknown or ambiguous: {model.identity}"
                )
            return RunOverride(
                model=ModelOverride(identity=resolved[0], effort=model.effort)
            )
        return update
    if command in {"agic", "flow", "runnable"} and update.runnable != "default":
        kind = "runnable" if command == "runnable" else command
        resolved = chat_slashes._resolve_runnable_command(
            client.list_runnables(kind),
            update.runnable or "",
            kind=kind,
        )
        if resolved is None:
            raise ValueError(
                f"Runnable selection is unknown or ambiguous: {update.runnable}"
            )
        return RunOverride(runnable=resolved)
    return update


class _ScriptedRunRenderer:
    """Render assistant text from one directly traced run."""

    def __init__(self) -> None:
        self._assistant_open = False
        self._text_delta_steps: set[StepPath] = set()
        self._terminal: RunEnd | None = None
        self._state_failure: str | None = None

    @property
    def failure(self) -> str | None:
        if self._state_failure is not None:
            return self._state_failure
        terminal = self._terminal
        if terminal is None or terminal.status == "succeeded":
            return None
        return execution_error_message(terminal.error) or f"run {terminal.status}"

    def reset(self) -> None:
        self._close()
        self._text_delta_steps.clear()
        self._terminal = None
        self._state_failure = None

    def render(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin):
            self._text_delta_steps.clear()
            self._terminal = None
            return
        if isinstance(event, PartDelta):
            if not isinstance(event.delta, TextDelta) or not event.delta.text:
                return
            self._text_delta_steps.add(event.step)
            self._write(event.delta.text)
            return
        if isinstance(event, StepEnd):
            if event.kind != "model" or event.step in self._text_delta_steps:
                return
            value = event.output.value if event.output is not None else ()
            parts = value if isinstance(value, Array | tuple | list) else ()
            text = message_text(
                tuple(part for part in parts if isinstance(part, TextPart))
            ).strip()
            if text:
                self._write(text)
            return
        if isinstance(event, RunEnd):
            self._terminal = event
            self._close()

    def handle_state(self, state: ChatRunState) -> None:
        if isinstance(state, RunBlocked):
            self._state_failure = state.message
            self._close()
            return
        if not isinstance(state, RunRecovered):
            return
        detail = state.detail
        if detail.status != "succeeded":
            self._state_failure = (
                execution_error_message(detail.error) or f"run {detail.status}"
            )
        self._close()

    def _write(self, text: str) -> None:
        if not self._assistant_open:
            typer.echo("assistant: ", nl=False)
            self._assistant_open = True
        typer.echo(text, nl=False)

    def _close(self) -> None:
        if self._assistant_open:
            typer.echo()
            self._assistant_open = False


def _target_thread_id(ctx: typer.Context, target: str | None) -> str | None:
    if target is None:
        return None
    if target.startswith("run_"):
        with open_execution(ctx, required=True) as resources:
            if resources is None:  # pragma: no cover
                raise RuntimeError("execution resources were not opened")
            run = RunHistory(resources.store).get_run(target)
        if run is None:
            raise click.ClickException(f"run not found: {target}")
        return run.thread_id
    return target


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
