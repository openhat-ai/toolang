"""Process-local terminal chat commands."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import sys

import click
import typer

from toolang.base.types.message import TextDelta, TextPart, message_text
from toolang.common.errors import ToolangError
from toolang.execution.events import PartDelta, RunBegin, RunEnd, RunEvent, StepEnd
from toolang.execution.history import RunHistory
from toolang.plugin.models.resolution import split_model_selectors
from toolang.plugin.tools.registry import split_tool_selectors
from toolang.state.state import split_cap_selectors

from toolang.cli.common.context import context_layout
from toolang.cli.common.execution import open_execution
from . import slashes as chat_slashes
from .base import ChatClient, friendly_error as chat_friendly_error
from .history import ChatInputHistoryStore
from .local import LocalChatSession
from .tui import ChatTuiApp


def chat_command(
    ctx: typer.Context,
    thread: str | None = None,
    models: list[str] | None = None,
    model: str | None = None,
    tools: list[str] | None = None,
    caps: list[str] | None = None,
    agic: str | None = None,
    flow: str | None = None,
    sandbox: str | None = None,
) -> None:
    thread_id = _target_thread_id(ctx, thread) if thread is not None else None
    selectors = _chat_selector_payload(
        models=models,
        model=model,
        tools=tools,
        caps=caps,
        agic=agic,
        flow=flow,
    )
    _chat_interactive(
        ctx,
        thread_id=thread_id,
        selector_payload=selectors,
        sandbox=sandbox,
    )


def send_command(
    ctx: typer.Context,
    thread: str,
    message: str,
    model: str | None = None,
) -> None:
    target = _target_thread_id(ctx, thread)
    if target is None:  # pragma: no cover - the CLI argument is required
        raise click.ClickException("thread id is required")
    selectors: dict[str, object] = {}
    if model is not None:
        selectors["model"] = model
    renderer = _ScriptedRunRenderer()
    errors: list[str] = []
    with _chat_runtime(ctx, sandbox=None, selector_payload=selectors) as client:
        client.start_run(
            target,
            message,
            selectors,
            renderer.render,
            errors.append,
        )
    if errors:
        raise click.ClickException(chat_friendly_error(errors[-1]))
    if renderer.failure:
        raise click.ClickException(chat_friendly_error(renderer.failure))


def attach_command(
    ctx: typer.Context,
    thread: str,
) -> None:
    _chat_interactive(ctx, thread_id=_target_thread_id(ctx, thread))


def _chat_selector_payload(
    *,
    models: list[str] | None,
    model: str | None = None,
    tools: list[str] | None,
    caps: list[str] | None,
    agic: str | None = None,
    flow: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if agic is not None and flow is not None:
        raise click.ClickException("--agic and --flow cannot be used together")
    model_selectors = tuple(dict.fromkeys(split_model_selectors(tuple(models or ()))))
    if model_selectors:
        payload["models"] = list(model_selectors)
    if model is not None:
        payload["model"] = model
    if tools is not None:
        tool_selectors = tuple(dict.fromkeys(split_tool_selectors(tuple(tools))))
        payload["tools"] = list(tool_selectors)
    cap_selectors = tuple(dict.fromkeys(split_cap_selectors(tuple(caps or ()))))
    if cap_selectors:
        payload["caps"] = list(cap_selectors)
    if agic is not None:
        payload["agic"] = agic
    if flow is not None:
        payload["flow"] = flow
    return payload


def _chat_interactive(
    ctx: typer.Context,
    *,
    thread_id: str | None,
    selector_payload: dict[str, object] | None = None,
    sandbox: str | None = None,
) -> None:
    selectors = dict(selector_payload or {})
    with _chat_runtime(
        ctx,
        sandbox=sandbox,
        selector_payload=selectors,
    ) as client:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            _chat_interactive_scripted_local(
                client=client,
                thread_id=thread_id,
                selector_payload=selectors,
            )
            return
        _chat_interactive_prompt_toolkit(
            ctx,
            thread_id=thread_id,
            selector_payload=selectors,
            client=client,
        )


@contextmanager
def _chat_runtime(
    ctx: typer.Context,
    *,
    sandbox: str | None,
    selector_payload: Mapping[str, object] | None = None,
) -> Iterator[ChatClient]:
    """Own one process-local execution session for this chat command."""

    if sandbox is not None and sandbox.partition(":")[0].strip() != "none":
        raise click.ClickException(
            "direct chat execution currently supports only the none sandbox"
        )
    layout = context_layout(ctx)
    selectors = selector_payload or {}
    local = LocalChatSession(
        layout,
        models=_strings(selectors.get("models")),
        tools=(_strings(selectors.get("tools")) if "tools" in selectors else None),
        caps=_strings(selectors.get("caps")),
    )
    try:
        yield local
    finally:
        local.close()


def _chat_input_history_store(ctx: typer.Context) -> ChatInputHistoryStore | None:
    try:
        layout = context_layout(ctx)
    except (AttributeError, KeyError, TypeError):
        return None
    return ChatInputHistoryStore(
        layout.runtime / "chat-input-history.jsonl"
    )


def _chat_home_label(ctx: typer.Context) -> str:
    try:
        return str(context_layout(ctx).home)
    except Exception:
        return "agent home"


def _chat_interactive_prompt_toolkit(
    ctx: typer.Context,
    *,
    thread_id: str | None,
    selector_payload: dict[str, object] | None = None,
    client: ChatClient,
) -> None:
    ChatTuiApp.run(
        thread_id=thread_id,
        selects=dict(selector_payload or {}),
        home=_chat_home_label(ctx),
        input_history=_chat_input_history_store(ctx),
        client=client,
    )


def _chat_resolve_model_command(
    selector: str,
    *,
    client: ChatClient,
) -> tuple[str, str] | None:
    try:
        payload = client.list_models()
    except (click.ClickException, ToolangError, ValueError):
        return None
    return chat_slashes._chat_resolve_model_command(payload, selector)


def _chat_interactive_scripted_local(
    *,
    client: ChatClient,
    thread_id: str | None,
    selector_payload: dict[str, object] | None = None,
) -> None:
    selectors = dict(selector_payload or {})
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
        if _chat_handle_scripted_command(
            text,
            selectors,
            client=client,
        ):
            continue
        errors: list[str] = []
        renderer.reset()
        client.start_run(
            ensure_thread_id(),
            text,
            selectors,
            renderer.render,
            errors.append,
        )
        failure = errors[-1] if errors else renderer.failure
        if failure:
            typer.echo(chat_friendly_error(failure), err=True)


def _chat_handle_scripted_command(
    message: str,
    selector_payload: dict[str, object],
    *,
    client: ChatClient,
) -> bool:
    parsed = chat_slashes._chat_local_command(message)
    if parsed is None:
        return False
    command, argument = parsed
    if command in {"help", "?"}:
        for line in chat_slashes._chat_help_lines():
            typer.echo(line)
        return True
    if command in {"agic", "flow"}:
        return _chat_handle_scripted_executable_command(
            command,
            argument,
            selector_payload,
            client=client,
        )
    if command not in {"model", "models"}:
        typer.echo(f"Unknown command: /{command}")
        return True
    if argument:
        selectors = chat_slashes._chat_model_command_selectors(argument)
        if len(selectors) != 1:
            typer.echo("/model requires exactly one selector")
            return True
        resolved = _chat_resolve_model_command(selectors[0], client=client)
        if resolved is None:
            typer.echo(f"Model selector must match exactly one model: {selectors[0]}")
            return True
        selector, label = resolved
        selector_payload["model"] = selector
        typer.echo(f"model: {label}")
        return True
    try:
        payload = client.list_models()
    except (click.ClickException, ToolangError, ValueError) as exc:
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        typer.echo(chat_friendly_error(message))
        return True
    typer.echo("available models")
    for line in chat_slashes._chat_model_list_lines(payload):
        typer.echo(line)
    return True


def _chat_handle_scripted_executable_command(
    command: str,
    argument: str,
    selector_payload: dict[str, object],
    *,
    client: ChatClient,
) -> bool:
    if argument:
        chat_slashes._chat_set_executable_selector(
            selector_payload,
            kind=command,
            name=argument,
        )
        typer.echo(f"{command}: {argument}")
        return True
    try:
        payload = client.list_executables(command)
    except (click.ClickException, ToolangError, ValueError) as exc:
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        typer.echo(chat_friendly_error(message))
        return True
    selected = _text(selector_payload.get(command))
    typer.echo(f"available {command}s")
    for line in chat_slashes._chat_executable_list_lines(payload, selected=selected):
        typer.echo(line)
    return True


class _ScriptedRunRenderer:
    """Render assistant text from one directly traced run."""

    def __init__(self) -> None:
        self._assistant_open = False
        self._text_delta_steps: set[str] = set()
        self._terminal: RunEnd | None = None

    @property
    def failure(self) -> str | None:
        terminal = self._terminal
        if terminal is None or terminal.status == "finished":
            return None
        return terminal.error or f"run {terminal.status}"

    def reset(self) -> None:
        self._close()
        self._text_delta_steps.clear()
        self._terminal = None

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
            text = message_text(
                tuple(part for part in event.output if isinstance(part, TextPart))
            ).strip()
            if text:
                self._write(text)
            return
        if isinstance(event, RunEnd):
            self._terminal = event
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


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
