"""Chat, thread, and run commands."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
import os
import sys
import threading
from typing import Annotated, Any, cast
from urllib.parse import urlencode
from uuid import uuid4

import click
import typer

from toolang.up import process as agents
from toolang.up import server as agent_up
from toolang.base.types.sandbox import SandboxSelector
from toolang.common.layout import AgentLayout
from toolang.common.errors import ToolangError
from toolang.execution.history import RunHistory
from toolang.execution.stream import trace_event_data
from toolang.state.state import split_cap_selectors
from toolang.plugin.models.resolution import split_model_selectors
from toolang.plugin.tools.registry import split_tool_selectors
from ...impl.chat import slashes as chat_slashes
from ...impl.chat.base import ChatClient, friendly_error as chat_friendly_error
from ...impl.chat.history import ChatInputHistoryStore
from ...impl.chat.local import LocalChatSession
from ...impl.chat.tui import ChatTuiApp
from ...common.client import (
    RuntimeClient,
    message_payload,
    owned_runtime_client,
    runtime_client,
    runtime_get,
    runtime_post,
)
from ...common.errors import RuntimeClientError
from ...common.context import (
    context_agent,
    context_root,
    load_runtime_environ,
    require_prefix_agent,
    ui_base_url,
)
from ...common.execution import open_execution


def chat_command(
    ctx: typer.Context,
    thread: Annotated[
        str | None,
        typer.Argument(
            help="Thread id to continue. Run id also accepted. Omit to start a new one.",
            metavar="THREAD",
        ),
    ] = None,
    models: Annotated[
        list[str] | None,
        typer.Option(
            "--models",
            help="Limit available models. Pass CSV or repeat.",
        ),
    ] = None,
    tools: Annotated[
        list[str] | None,
        typer.Option(
            "--tools",
            help="Allow selected tools. Pass CSV or repeat.",
        ),
    ] = None,
    caps: Annotated[
        list[str] | None,
        typer.Option(
            "--caps",
            help="Allow selected caps. Pass CSV or repeat.",
        ),
    ] = None,
    agic: Annotated[
        str | None, typer.Option("--agic", help="Use an agic for new runs.")
    ] = None,
    flow: Annotated[
        str | None, typer.Option("--flow", help="Use a flow for new runs.")
    ] = None,
    sandbox: Annotated[
        str | None,
        typer.Option(
            "--sandbox",
            help="Execute the session in this sandbox when no API is running.",
        ),
    ] = None,
) -> None:
    thread_id = _target_thread_id(ctx, thread) if thread is not None else None
    selectors = _chat_selector_payload(
        models=models, tools=tools, caps=caps, agic=agic, flow=flow
    )
    _chat_interactive(
        ctx,
        thread_id=thread_id,
        selector_payload=selectors,
        sandbox=sandbox,
    )


def send_command(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
    message: Annotated[str, typer.Argument(help="Message text.")],
    model: Annotated[
        str | None, typer.Option("--model", help="Model selector.")
    ] = None,
) -> None:
    target = _target_thread_id(ctx, thread)
    payload: dict[str, Any] = {
        "thread": target,
        "client": "tui",
        "message": message_payload(message),
    }
    if model is not None:
        payload["model"] = model
    _runtime_stream(ctx, "/api/v1/chat/stream", payload=payload)


def attach_command(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
) -> None:
    _open_thread_ui(ctx, _target_thread_id(ctx, thread))


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    if value is None:
        return None
    return None


def _runtime_stream(ctx: typer.Context, path: str, *, payload: dict[str, Any]) -> None:
    try:
        for line in runtime_client(ctx).lines(path, payload=payload):
            typer.echo(line)
    except RuntimeClientError as exc:
        raise click.ClickException(str(exc)) from exc


def _runtime_get_stream(ctx: typer.Context, path: str) -> None:
    try:
        for line in runtime_client(ctx).lines(path):
            typer.echo(line)
    except RuntimeClientError as exc:
        raise click.ClickException(str(exc)) from exc


def _runtime_consume_stream(
    ctx: typer.Context,
    path: str,
    *,
    payload: dict[str, Any],
    event_handler: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    try:
        runtime_client(ctx).consume(path, payload=payload, on_event=event_handler)
    except RuntimeClientError as exc:
        raise click.ClickException(str(exc)) from exc


def _chat_selector_payload(
    *,
    models: list[str] | None,
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


def _open_thread_ui(
    ctx: typer.Context,
    thread_id: str | None,
    *,
    selector_payload: dict[str, object] | None = None,
) -> None:
    _chat_interactive(ctx, thread_id=thread_id, selector_payload=selector_payload)


def _chat_interactive(
    ctx: typer.Context,
    *,
    thread_id: str | None,
    selector_payload: dict[str, object] | None = None,
    sandbox: str | None = None,
) -> None:
    with _chat_runtime(ctx, sandbox=sandbox) as client:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            if isinstance(client, RuntimeClient):
                _chat_interactive_scripted(
                    ctx, thread_id=thread_id, selector_payload=selector_payload
                )
            else:
                _chat_interactive_scripted_local(
                    ctx,
                    client=client,
                    thread_id=thread_id,
                    selector_payload=selector_payload,
                )
            return
        _chat_interactive_prompt_toolkit(
            ctx,
            thread_id=thread_id,
            selector_payload=selector_payload,
            client=client,
        )


@contextmanager
def _chat_runtime(ctx: typer.Context, *, sandbox: str | None) -> Iterator[ChatClient]:
    """Reuse an API or own the selected execution host for this chat session."""

    root = context_root(ctx)
    name = require_prefix_agent(ctx)
    layout = AgentLayout.resident(root, name)
    existing = agents.AgentProcess(layout).status(ui_base_url=ui_base_url())
    if (
        existing is not None
        and existing.status == "running"
        and existing.endpoint is not None
    ):
        if sandbox is not None:
            _validate_running_sandbox(existing, sandbox)
        yield RuntimeClient(existing.endpoint)
        return

    from . import runtime as runtime_commands

    if existing is not None and existing.status in {"running", "preparing", "starting"}:
        raise click.ClickException(runtime_commands.active_agent_error(existing))
    environ = load_runtime_environ(layout, base_environ=os.environ)
    environ["TOOLANG_ROOT"] = str(root)
    agent_state = agent_up.prepare_agent(layout=layout)
    agent_hosting = agent_up.resolve_agent_hosting(
        agent_state,
        sandbox=sandbox,
        environ=environ,
    )
    if agent_hosting.selector.driver == "none":
        local = LocalChatSession(
            layout,
            environ=environ,
            agent_state=agent_state,
        )
        try:
            yield local
        finally:
            local.close()
        return

    launch = runtime_commands.resolve_startup(
        ctx,
        layout,
        sandbox=agent_hosting.selector.render(),
        models=None,
        tools=None,
        caps=None,
        inboxes=None,
        port=None,
        host="127.0.0.1",
        endpoint_host=None,
        dev=None,
        background=True,
        progress=None,
    )
    if launch.log_plan.path is None:
        raise click.ClickException("agent log path was not resolved")
    with owned_runtime_client(
        layout=layout,
        startup=launch.startup,
        environ=launch.environ,
        log_path=launch.log_plan.path,
    ) as client:
        yield client


def _validate_running_sandbox(status: agents.AgentStatus, requested: str) -> None:
    selector = SandboxSelector.parse(requested)
    actual = status.sandbox
    matches = actual == selector.render()
    if selector.target is None and isinstance(actual, str):
        matches = actual.partition(":")[0] == selector.driver
    if not matches:
        raise click.ClickException(
            f"agent is already running in sandbox {actual or 'unknown'}; "
            f"cannot use {selector.render()} for this chat"
        )


def _chat_input_history_store(ctx: typer.Context) -> ChatInputHistoryStore | None:
    try:
        agent = context_agent(ctx)
        root = context_root(ctx)
    except (AttributeError, KeyError, TypeError):
        return None
    if not agent:
        return None
    return ChatInputHistoryStore(
        AgentLayout.resident(root, agent).runtime / "chat-input-history.jsonl"
    )


def _chat_home_label(ctx: typer.Context) -> str:
    try:
        agent_name = context_agent(ctx)
        if agent_name is None:
            return "agent home"
        return str(AgentLayout.resident(context_root(ctx), agent_name).home)
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


def _chat_resolve_model_command_labels(
    ctx: typer.Context,
    selectors: Sequence[str],
    *,
    client: ChatClient | None = None,
) -> tuple[str, ...] | None:
    try:
        payload = (
            client.list_models()
            if client is not None
            else runtime_get(ctx, "/api/v1/models")
        )
    except (click.ClickException, RuntimeClientError, ToolangError, ValueError):
        return None
    return chat_slashes._chat_resolve_model_command_labels(payload, selectors)


def _chat_interactive_scripted(
    ctx: typer.Context,
    *,
    thread_id: str | None,
    selector_payload: dict[str, object] | None = None,
) -> None:
    selectors = dict(selector_payload or {})
    local_streaming = threading.Event()
    local_request_ids: set[str] = set()
    listener: _ThreadEventListener | None = None

    def ensure_thread_id() -> str:
        nonlocal listener, thread_id
        if thread_id is None:
            result = runtime_post(ctx, "/api/v1/threads", payload={"client": "tui"})
            created = _result_thread_id(result)
            if created is None:
                raise click.ClickException("runtime did not return a thread id")
            thread_id = created
            typer.echo(f"thread {thread_id}")
        if listener is None:
            listener = _start_thread_event_listener(
                ctx,
                thread_id,
                local_streaming=local_streaming,
                local_request_ids=local_request_ids,
            )
        return thread_id

    if thread_id is not None:
        typer.echo(f"thread {thread_id}")
        ensure_thread_id()
    try:
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
            if _chat_handle_scripted_command(ctx, text, selectors):
                continue
            active_thread_id = ensure_thread_id()
            request_id = f"term_{uuid4().hex}"
            local_request_ids.add(request_id)
            payload: dict[str, Any] = {
                "thread": active_thread_id,
                "client": "tui",
                "request_id": request_id,
                "message": message_payload(text),
                **selectors,
            }
            local_streaming.set()
            try:
                _runtime_consume_stream(ctx, "/api/v1/chat/stream", payload=payload)
            finally:
                local_streaming.clear()
                local_request_ids.discard(request_id)
    finally:
        if listener is not None:
            listener.stop()


def _result_thread_id(result: Mapping[str, object]) -> str | None:
    thread = result.get("thread")
    if isinstance(thread, Mapping):
        thread_id = cast(Mapping[str, object], thread).get("id")
        if isinstance(thread_id, str) and thread_id:
            return thread_id
    legacy = result.get("thread_id")
    return legacy if isinstance(legacy, str) and legacy else None


def _chat_interactive_scripted_local(
    ctx: typer.Context,
    *,
    client: ChatClient,
    thread_id: str | None,
    selector_payload: dict[str, object] | None = None,
) -> None:
    selectors = dict(selector_payload or {})
    renderer = _ThreadEventRenderer()

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
            ctx,
            text,
            selectors,
            client=client,
        ):
            continue
        client.start_run(
            ensure_thread_id(),
            text,
            selectors,
            lambda event: renderer.render(trace_event_data(event)),
            lambda error: typer.echo(chat_friendly_error(error), err=True),
        )


def _chat_handle_scripted_command(
    ctx: typer.Context,
    message: str,
    selector_payload: dict[str, object],
    *,
    client: ChatClient | None = None,
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
            ctx, command, argument, selector_payload, client=client
        )
    if command not in {"model", "models"}:
        typer.echo(f"Unknown command: /{command}")
        return True
    if argument:
        selectors = chat_slashes._chat_model_command_selectors(argument)
        if not selectors:
            typer.echo("/model requires a selector")
            return True
        labels = _chat_resolve_model_command_labels(ctx, selectors, client=client)
        if labels is None:
            typer.echo(f"Model selector matched no models: {', '.join(selectors)}")
            return True
        selector_payload["models"] = list(selectors)
        typer.echo(f"model: {', '.join(labels)}")
        return True
    try:
        payload = (
            client.list_models()
            if client is not None
            else runtime_get(ctx, "/api/v1/models")
        )
    except (click.ClickException, RuntimeClientError, ToolangError, ValueError) as exc:
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        typer.echo(chat_friendly_error(message))
        return True
    typer.echo("available models")
    for line in chat_slashes._chat_model_list_lines(payload):
        typer.echo(line)
    return True


def _chat_handle_scripted_executable_command(
    ctx: typer.Context,
    command: str,
    argument: str,
    selector_payload: dict[str, object],
    *,
    client: ChatClient | None = None,
) -> bool:
    if argument:
        chat_slashes._chat_set_executable_selector(
            selector_payload, kind=command, name=argument
        )
        typer.echo(f"{command}: {argument}")
        return True
    try:
        payload = (
            client.list_executables(command)
            if client is not None
            else runtime_get(ctx, f"/api/v1/chat/{command}s")
        )
    except (click.ClickException, RuntimeClientError, ToolangError, ValueError) as exc:
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        typer.echo(chat_friendly_error(message))
        return True
    selected = _text(selector_payload.get(command))
    typer.echo(f"available {command}s")
    for line in chat_slashes._chat_executable_list_lines(payload, selected=selected):
        typer.echo(line)
    return True


class _ThreadEventListener:
    def __init__(self, stop_event: threading.Event) -> None:
        self._stop_event = stop_event

    def stop(self) -> None:
        self._stop_event.set()


def _start_thread_event_listener(
    ctx: typer.Context,
    thread_id: str,
    *,
    local_streaming: threading.Event | None = None,
    local_request_ids: set[str] | None = None,
    redraw_prompt: bool = True,
    event_handler: Callable[[dict[str, Any]], None] | None = None,
) -> _ThreadEventListener:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run_thread_event_listener_from_cursor,
        args=(
            ctx,
            thread_id,
            stop_event,
            local_streaming,
            local_request_ids,
            redraw_prompt,
            event_handler,
        ),
        daemon=True,
    )
    thread.start()
    return _ThreadEventListener(stop_event)


def _thread_event_cursor(ctx: typer.Context, thread_id: str) -> int | None:
    detail = runtime_get(ctx, f"/api/v1/threads/{thread_id}")
    cursor = detail.get("event_cursor")
    if isinstance(cursor, int):
        return cursor
    return None


def _run_thread_event_listener_from_cursor(
    ctx: typer.Context,
    thread_id: str,
    stop_event: threading.Event,
    local_streaming: threading.Event | None,
    local_request_ids: set[str] | None,
    redraw_prompt: bool,
    event_handler: Callable[[dict[str, Any]], None] | None,
) -> None:
    try:
        after = _thread_event_cursor(ctx, thread_id)
    except click.ClickException:
        return
    if stop_event.is_set():
        return
    _run_thread_event_listener(
        ctx,
        thread_id,
        after,
        stop_event,
        local_streaming,
        local_request_ids,
        redraw_prompt,
        event_handler,
    )


def _run_thread_event_listener(
    ctx: typer.Context,
    thread_id: str,
    after: int | None,
    stop_event: threading.Event,
    local_streaming: threading.Event | None,
    local_request_ids: set[str] | None,
    redraw_prompt: bool,
    event_handler: Callable[[dict[str, Any]], None] | None,
) -> None:
    renderer = _ThreadEventRenderer(
        redraw_prompt=redraw_prompt,
        local_streaming=local_streaming,
        local_request_ids=local_request_ids,
    )
    path = f"/api/v1/threads/{thread_id}/stream"
    if after is not None:
        path = f"{path}?{urlencode([('after', str(after))])}"
    try:
        for event in runtime_client(ctx).events(path, stop=stop_event):
            if stop_event.is_set():
                return
            if event_handler is not None:
                event_handler(event)
            else:
                renderer.render(event)
    except Exception:
        if not stop_event.is_set():
            typer.echo("thread event stream closed", err=True)


class _ThreadEventRenderer:
    def __init__(
        self,
        *,
        redraw_prompt: bool = False,
        local_streaming: threading.Event | None = None,
        local_request_ids: set[str] | None = None,
    ) -> None:
        self._assistant_open = False
        self._redraw_prompt = redraw_prompt
        self._local_streaming = local_streaming
        self._local_request_ids = local_request_ids
        self._local_run_ids: set[str] = set()
        self._text_delta_runs: set[str] = set()

    def render(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or event.get("event_type") or "")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if event_type == "run_starting":
            self._render_run_starting(payload)
        elif event_type == "part_delta":
            self._render_part_delta(payload)
        elif event_type == "step_end":
            self._render_step_end(payload)
        elif event_type in {"part_end", "run_end"}:
            self._close_assistant(
                redraw_prompt=event_type == "run_end",
                run_id=str(payload.get("run_id") or "") or None,
            )

    def _render_run_starting(self, payload: dict[str, Any]) -> None:
        self._remember_local_run(payload)
        text = _event_message_text(payload.get("input"))
        if not text:
            return
        self._close_assistant(
            redraw_prompt=False, run_id=str(payload.get("run_id") or "") or None
        )
        typer.echo(f"\nuser: {text}")

    def _render_part_delta(self, payload: dict[str, Any]) -> None:
        delta = payload.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "text":
            return
        text = str(delta.get("text") or "")
        if not text:
            return
        run_id = payload.get("run_id")
        if isinstance(run_id, str):
            self._text_delta_runs.add(run_id)
        if not self._assistant_open:
            typer.echo("assistant: ", nl=False)
            self._assistant_open = True
        typer.echo(text, nl=False)

    def _render_step_end(self, payload: dict[str, Any]) -> None:
        if payload.get("kind") != "model":
            return
        run_id = payload.get("run_id")
        if isinstance(run_id, str) and run_id in self._text_delta_runs:
            return
        text = _event_parts_text(payload.get("output"))
        if not text:
            return
        if not self._assistant_open:
            typer.echo("assistant: ", nl=False)
            self._assistant_open = True
        typer.echo(text, nl=False)

    def _close_assistant(self, *, redraw_prompt: bool, run_id: str | None) -> None:
        if self._assistant_open:
            typer.echo()
            self._assistant_open = False
        local_run = run_id is not None and run_id in self._local_run_ids
        if (
            redraw_prompt
            and self._redraw_prompt
            and not self._local_run_active(run_id=run_id)
        ):
            typer.echo("> ", nl=False)
        if redraw_prompt and local_run and run_id is not None:
            self._local_run_ids.discard(run_id)

    def _remember_local_run(self, payload: dict[str, Any]) -> None:
        request_id = payload.get("request_id")
        run_id = payload.get("run_id")
        if not isinstance(request_id, str) or not isinstance(run_id, str):
            return
        if (
            self._local_request_ids is not None
            and request_id in self._local_request_ids
        ):
            self._local_run_ids.add(run_id)

    def _local_run_active(self, *, run_id: str | None) -> bool:
        if run_id is not None and run_id in self._local_run_ids:
            return True
        if self._local_streaming is not None and self._local_streaming.is_set():
            return True
        return False


def _event_message_text(message: object) -> str:
    if not isinstance(message, Mapping):
        return ""
    typed_message = cast(Mapping[str, object], message)
    parts = typed_message.get("parts")
    if not isinstance(parts, list):
        return ""
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        typed_part = cast(Mapping[str, object], part)
        if typed_part.get("type") == "text":
            texts.append(str(typed_part.get("text") or ""))
    return "".join(texts).strip()


def _event_parts_text(parts: object) -> str:
    if not isinstance(parts, list):
        return ""
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        typed_part = cast(Mapping[str, object], part)
        if typed_part.get("type") == "text":
            texts.append(str(typed_part.get("text") or ""))
    return "".join(texts).strip()


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
