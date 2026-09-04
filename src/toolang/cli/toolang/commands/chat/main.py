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

from toolang.base.types.model import ModelOverride
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
from toolang.execution.policy import merge_run_overrides
from toolang.execution.types import (
    AllowField,
    AllowOverride,
    LimitField,
    LimitOverride,
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
from toolang.cli.common.execution_progress.config import (
    DEFAULT_MAX_PROGRESS_WIDTH,
    resolve_progress_max_width,
)
from toolang.cli.common.execution_progress.formatting import wrap_display
from toolang.cli.common.human_values import parts_response_text
from toolang.cli.common.output import shorten_home_path
from toolang.cli.common.terminal_surfaces import resolve_terminal_surfaces
from . import slashes as chat_slashes
from .base import (
    AppContext,
    ChatClient,
    ChatRunState,
    RunBlocked,
    RunRecovered,
    friendly_error as chat_friendly_error,
)
from .blocks import MutableBlock
from .history import ChatInputHistoryStore
from .input import (
    QuickCommand,
    RunOverrideHelp,
    is_runnable_input,
    is_slash_input,
    normalize_chat_input,
    parse_chat_input,
    slash_command_name,
)
from .local import LocalChatSession
from .presenter import ChatRunPresenter
from .policy import run_override_error
from .remote import RemoteChatError, RemoteChatSession
from .rendering import terminal_width
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
        initial_update, clear_runnable = _chat_session_override(
            allow_options=allow_options,
            default_options=default_options,
            limit_options=limit_options,
        )
        if not initial_update.empty:
            setting = client.apply_setting(setting, initial_update)
        if clear_runnable:
            setting = replace(setting, runnable=None)
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            _chat_interactive_scripted_local(
                client=client,
                thread_id=thread_id,
                setting=setting,
                progress_max_width=user_call(
                    resolve_progress_max_width,
                    load_runtime_environ(context_layout(ctx), base_environ=os.environ),
                ),
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
) -> tuple[RunOverride, bool]:
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
    model = defaults.get("model")
    if model is not None and not isinstance(model, ModelOverride):
        raise TypeError("--default model must resolve to a model override")
    runnable = defaults.get("runnable")
    if isinstance(runnable, ModelOverride):
        raise TypeError("--default runnable must resolve to a string or none")
    clear_runnable = runnable is None and "runnable" in defaults
    if model is not None or runnable is not None:
        updates.append(
            RunOverride(
                model=model,
                runnable=runnable,
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
    return merge_run_overrides(updates), clear_runnable


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
    environ = load_runtime_environ(context_layout(ctx), base_environ=os.environ)
    ChatTuiApp.run(
        thread_id=thread_id,
        setting=setting,
        home=_chat_home_label(ctx),
        input_history=_chat_input_history_store(ctx),
        client=client,
        progress_max_width=user_call(
            resolve_progress_max_width,
            environ,
        ),
        surfaces=user_call(resolve_terminal_surfaces, environment=environ),
    )


def _chat_interactive_scripted_local(
    *,
    client: ChatClient,
    thread_id: str | None,
    setting: SessionSetting,
    progress_max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
) -> None:
    renderer = _ScriptedRunRenderer()
    context = _ScriptedAppContext(
        client,
        setting=setting,
        thread_id=thread_id,
        progress_max_width=progress_max_width,
    )

    def ensure_thread_id() -> str:
        existing = context.get_thread_id()
        resolved = context.ensure_thread_id()
        if existing is None:
            typer.echo(f"thread {resolved}")
        return resolved

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
        if not text.strip():
            continue
        source = normalize_chat_input(text)
        try:
            chat_input = parse_chat_input(source)
        except ValueError as exc:
            if is_slash_input(source):
                command = slash_command_name(source) or ""
                if chat_slashes.is_registered(command):
                    _echo_scripted_outcome(
                        chat_slashes.error_outcome(str(exc)),
                        max_width=progress_max_width,
                    )
                else:
                    typer.echo(
                        chat_slashes.unrecognized_diagnostic(command),
                        err=True,
                    )
            else:
                error = chat_friendly_error(str(exc))
                typer.echo(
                    run_override_error(source, error)
                    if source.startswith(":")
                    else error,
                    err=True,
                )
            continue
        if isinstance(chat_input, QuickCommand):
            if not chat_slashes.is_registered(chat_input.name):
                typer.echo(
                    chat_slashes.unrecognized_diagnostic(chat_input.name),
                    err=True,
                )
                continue
            outcome = chat_slashes.handle(context, chat_input)
            if outcome is not None:
                _echo_scripted_outcome(outcome, max_width=progress_max_width)
            if context.exit_requested:
                return
            continue
        if isinstance(chat_input, RunOverrideHelp):
            _echo_scripted_outcome(
                chat_slashes.run_override_help(),
                max_width=progress_max_width,
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
                context.get_setting(),
            )
        except (click.ClickException, ToolangError, ValueError) as exc:
            detail = exc.message if isinstance(exc, click.ClickException) else str(exc)
            typer.echo(chat_friendly_error(detail), err=True)
            continue
        client.run(request, renderer.render, errors.append, renderer.handle_state)
        failure = errors[-1] if errors else renderer.failure
        if failure:
            typer.echo(chat_friendly_error(failure), err=True)


class _ScriptedAppContext(AppContext):
    """Minimal slash-command context for line-oriented Chat."""

    def __init__(
        self,
        client: ChatClient,
        *,
        setting: SessionSetting,
        thread_id: str | None,
        progress_max_width: int,
    ) -> None:
        self.client = client
        self.setting = setting
        self.thread_id = thread_id
        self.live_blocks: list[MutableBlock] = []
        self.presenter = ChatRunPresenter(max_width=progress_max_width)
        self.exit_requested = False

    def get_setting(self) -> SessionSetting:
        return self.setting

    def set_setting(self, setting: SessionSetting) -> None:
        self.setting = setting

    def get_client(self) -> ChatClient:
        return self.client

    def get_active_run(self) -> str | None:
        return None

    def get_thread_id(self) -> str | None:
        return self.thread_id

    def ensure_thread_id(self) -> str:
        if self.thread_id is None:
            self.thread_id = self.client.create_thread()
        return self.thread_id

    def set_active_run(self, run_id: str | None) -> None:
        del run_id

    def get_live_blocks(self) -> list[MutableBlock]:
        return self.live_blocks

    def get_presenter(self) -> ChatRunPresenter:
        return self.presenter

    def finalize_block(self, block: MutableBlock) -> None:
        if block in self.live_blocks:
            self.live_blocks.remove(block)

    def finish_run(self) -> None:
        return None

    def refresh_status(self) -> None:
        return None

    def request_exit(self) -> None:
        self.exit_requested = True


def _echo_scripted_outcome(
    outcome: chat_slashes.SlashOutcome,
    *,
    max_width: int,
) -> None:
    width = max(1, min(terminal_width(), max_width))
    content = outcome.content
    if isinstance(content, chat_slashes.SlashRunResult):
        for line in wrap_display(f"{content.result.run_id} output", width):
            typer.echo(line)
        text = parts_response_text(content.result.output)
        if text:
            for raw_line in text.splitlines() or [""]:
                for line in wrap_display(raw_line, width):
                    typer.echo(line)
        return
    for line in chat_slashes.outcome_lines(outcome, width=width):
        typer.echo(line, err=outcome.kind == "error")


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
