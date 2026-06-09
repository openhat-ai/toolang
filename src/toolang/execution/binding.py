"""Bind queued run requests to immutable runtime inputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, cast

from toolang.base.types.message import Message, message_text

from .. import agents
from ..plugin import normalize_run_loop_name
from ..state.live import LiveState
from ..common.ids import LOCAL_ID_FAMILY, RUN_ID_FAMILY, allocate_id
from .db import utc_now
from .records import RunLoop, ThreadPeer

if TYPE_CHECKING:
    from ..up import UptimeContext
    from .runner import RunRequest

_LOGGER = logging.getLogger("toolang.run")


@dataclass(frozen=True, slots=True)
class RunBinding:
    """One run bound to immutable live state and runtime ids."""

    run_id: str
    group: str
    origin: str
    thread_id: str
    thunk_name: str | None
    input_text: str
    message: Message | None
    model_selector: str | None
    run_loop: RunLoop
    metadata: dict[str, Any]
    live: LiveState
    created_at: str


def bind_run_request(
    context: UptimeContext,
    request: RunRequest,
    *,
    live: LiveState | None = None,
) -> RunBinding:
    """Bind one queued run request to immutable runtime inputs."""

    bound_live = live or context.live
    thread_id = request.thread_id or _request_thread_id(context, request)
    thread_peer = _request_thread_peer(request.metadata)
    existing_thread = context.store.get_thread(thread_id=thread_id)
    context.store.ensure_thread(
        thread_id=thread_id,
        origin=request.origin,
        peer=thread_peer,
    )
    if existing_thread is None:
        _LOGGER.info(
            "Thread created id=%s origin=%s source=%s",
            thread_id,
            request.origin,
            request.thread_kind or request.origin,
        )
    run_loop = normalize_run_loop_name(request.run_loop)
    return RunBinding(
        run_id=request.run_id or allocate_run_id(context),
        group=request.group,
        origin=request.origin,
        thread_id=thread_id,
        thunk_name=request.thunk_name,
        input_text=_request_input_text(request),
        message=request.message,
        model_selector=_request_model_selector(request),
        run_loop=run_loop,
        metadata=dict(request.metadata),
        live=bound_live,
        created_at=utc_now(),
    )


def allocate_run_id(context: UptimeContext) -> str:
    value = allocate_id(
        agents.agent_id_state_path(context.root, context.name),
        family=RUN_ID_FAMILY,
    ).value
    return f"run_{value}"


def run_selected_model_selector(run: RunBinding) -> str | None:
    if isinstance(run.model_selector, str) and run.model_selector.strip():
        return run.model_selector.strip()
    for key in ("model", "model_selector"):
        value = run.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def invoke_params(run: RunBinding) -> dict[str, Any]:
    value = run.metadata.get("invoke_params")
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def invoke_parts(run: RunBinding) -> tuple[dict[str, Any], ...]:
    value = run.metadata.get("invoke_parts")
    if not isinstance(value, list):
        return ()
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        items.append({str(key): part for key, part in item.items()})
    return tuple(items)


def _new_thread_id(context: UptimeContext, origin: str) -> str:
    value = allocate_id(
        agents.agent_id_state_path(context.root, context.name),
        family=LOCAL_ID_FAMILY,
    ).value
    return f"{_thread_id_kind(origin)}_{value}"


def _request_thread_id(context: UptimeContext, request: RunRequest) -> str:
    if request.origin == "script":
        return _new_thread_id(context, "script")
    return _new_thread_id(context, request.thread_kind or request.origin)


def _thread_id_kind(origin: str) -> str:
    text = "".join(char for char in origin.strip().lower() if char.isalnum())
    if text in {"web"}:
        return "web"
    if text in {"term", "terminal", "tui", "chat"}:
        return "term"
    if text in {"task"}:
        return "task"
    if text in {"chore"}:
        return "chore"
    return "script"


def _request_thread_peer(metadata: Mapping[str, Any]) -> ThreadPeer | None:
    raw = metadata.get("thread_peer")
    if not isinstance(raw, Mapping):
        return None
    return ThreadPeer.from_data(cast(Mapping[str, Any], raw))


def _request_input_text(request: RunRequest) -> str:
    if request.thunk:
        return request.thunk
    if request.message is None:
        return ""
    return message_text(request.message.parts)


def _request_model_selector(request: RunRequest) -> str | None:
    if isinstance(request.model_selector, str) and request.model_selector.strip():
        return request.model_selector.strip()
    return None
