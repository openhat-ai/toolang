"""Process-local terminal chat commands."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import shlex
import sys
from typing import cast

import typer
from typer._click.exceptions import ClickException

from toolang.base.types.model import ModelOverride
from toolang.base.types.message import TextDelta, TextPart, message_text
from toolang.cli.common.policy import (
    resolve_default_overrides,
    resolve_compact_override,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.common.errors import ToolangError
from toolang.execution.events import PartDelta, RunBegin, RunEnd, RunEvent, StepEnd
from toolang.execution.inspection.history import RunHistory
from toolang.execution.records import execution_error_message
from toolang.execution.policy import merge_run_overrides
from toolang.execution.types import (
    AllowField,
    AllowOverride,
    LimitField,
    LimitOverride,
    RunOverride,
    SessionSetting,
    StepRef,
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
from toolang.common.typer.options import BARE_VALUE
from toolang.cli.common.terminal_surfaces import resolve_terminal_surfaces
from toolang.cli.common.tmux import resolve_launcher, resolve_marks
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
from .marks import ChatMarks
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
    compact_model: str | None = None,
) -> None:
    thread_id = _target_thread_id(ctx, thread) if thread is not None else None
    if not _place_chat(ctx, thread_id=thread_id, argv=sys.argv):
        return
    _chat_interactive(
        ctx,
        thread_id=thread_id,
        model_catalog=model_catalog,
        sandbox=sandbox,
        dev=dev,
        allow_options=allows,
        default_options=defaults,
        limit_options=limits,
        compact_model=compact_model,
    )


def _place_chat(
    ctx: typer.Context,
    *,
    thread_id: str | None,
    argv: Sequence[str],
) -> bool:
    """Send this chat run to the agent's tmux session when tmux can host it.

    Returns ``True`` when chat keeps running in this process. ``False`` means
    the run now lives in the agent's session: the notice is printed and the
    caller must return, because the client points at another window.
    """

    agent = context_layout(ctx).name
    launcher = resolve_launcher(agent=agent)
    if launcher is None:
        return True
    session = launcher.agent_session()
    if session is not None and launcher.is_current(session):
        return True
    if session is not None and thread_id is not None:
        window = launcher.thread_window(session, thread_id)
        if window is not None:
            launcher.switch_client(window)
            _announce_session(agent)
            return False
    command = shlex.join(list(argv))
    directory = os.getcwd()
    session, window = launcher.ensure_session(command=command, directory=directory)
    if session is None:
        return True
    if window is None:
        window = launcher.open_window(session, command=command, directory=directory)
    if window is None:
        return True
    if not launcher.switch_client(window):
        launcher.close_window(window)
        return True
    _announce_session(agent)
    return False


def _announce_session(agent: str) -> None:
    """Tell the user where chat went: one line on stdout."""

    print(f"\u21aa opened in tmux session {agent}")


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
    compact_model: str | None = None,
) -> None:
    with _chat_runtime(
        ctx,
        model_catalog=model_catalog,
        sandbox=sandbox,
        dev=dev,
        compact_model=compact_model,
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
        marks = _chat_marks(context_layout(ctx).name, client)
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            marks.start(thread_id)
            try:
                _chat_interactive_scripted_local(
                    client=client,
                    thread_id=thread_id,
                    setting=setting,
                    marks=marks,
                    progress_max_width=user_call(
                        resolve_progress_max_width,
                        load_runtime_environ(
                            context_layout(ctx), base_environ=os.environ
                        ),
                    ),
                )
            finally:
                marks.clear()
            return
        _chat_interactive_prompt_toolkit(
            ctx,
            thread_id=thread_id,
            setting=setting,
            client=client,
            marks=marks,
        )


def _chat_marks(agent: str, client: ChatClient) -> ChatMarks:
    """Pane marks for one chat session; disabled outside tmux."""

    return ChatMarks(
        agent=agent,
        marks=resolve_marks(),
        title_lookup=client.thread_title,
    )


@contextmanager
def _chat_runtime(
    ctx: typer.Context,
    *,
    model_catalog: Path | None = None,
    sandbox: str | None,
    dev: Path | None = None,
    compact_model: str | None = None,
) -> Iterator[ChatClient]:
    """Own one local, attached, or temporary-remote Chat session."""

    layout = context_layout(ctx)
    compact_override = user_call(resolve_compact_override, {}, compact_model)
    try:
        server_context = acquire_agent_server(
            layout,
            sandbox=sandbox,
            dev=dev,
            model_catalog=resolve_model_catalog_option(model_catalog),
            ui_base_url=ui_base_url(),
            compact_override=compact_override,
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
                    raise ClickException(str(exc)) from exc
                try:
                    yield remote
                finally:
                    remote.close()
                return

            environ = load_runtime_environ(layout, base_environ=os.environ)
            local = LocalChatSession(
                layout,
                sandbox="host",
                compact_override=compact_override
                or user_call(resolve_compact_override, environ),
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
        raise ClickException(str(exc)) from exc


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
    marks: ChatMarks | None = None,
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
        marks=marks,
    )


def _chat_interactive_scripted_local(
    *,
    client: ChatClient,
    thread_id: str | None,
    setting: SessionSetting,
    marks: ChatMarks | None = None,
    progress_max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
) -> None:
    marks = marks if marks is not None else ChatMarks.disabled()
    renderer = _ScriptedRunRenderer(on_run_end=marks.refresh_title)
    context = _ScriptedAppContext(
        client,
        setting=setting,
        thread_id=thread_id,
        marks=marks,
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
        except (ClickException, ToolangError, ValueError) as exc:
            detail = exc.message if isinstance(exc, ClickException) else str(exc)
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
        marks: ChatMarks,
        progress_max_width: int,
    ) -> None:
        self.client = client
        self.setting = setting
        self.thread_id = thread_id
        self.marks = marks
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
            self.marks.set_thread(self.thread_id)
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

    def __init__(self, *, on_run_end: Callable[[], None] | None = None) -> None:
        self._on_run_end = on_run_end
        self._assistant_open = False
        self._text_delta_steps: set[StepRef] = set()
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
            value = event.output.local.value if event.output is not None else ()
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
            if self._on_run_end is not None:
                self._on_run_end()

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
    if target == "":
        raise typer.BadParameter("thread id must not be empty", param_hint="--thread")
    if target == BARE_VALUE:
        with open_execution(ctx) as resources:
            threads = (
                RunHistory(resources.store).list_threads(limit=1)
                if resources is not None
                else []
            )
        if not threads:
            raise ClickException(
                "no thread to resume; omit --thread to start a new session"
            )
        return threads[0].id
    if target.startswith("run_"):
        with open_execution(ctx, required=True) as resources:
            if resources is None:  # pragma: no cover
                raise RuntimeError("execution resources were not opened")
            run = RunHistory(resources.store).get_run(target)
        if run is None:
            raise ClickException(f"run not found: {target}")
        return run.thread_id
    return target


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
