"""Bind run requests to immutable runtime inputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any, cast

from toolang.base.types.message import Message, message_text

from toolang.agent import local as agents
from toolang.plugin.loading import normalize_run_loop_name
from ..state.agent import AgentState
from ..common.ids import LOCAL_ID_FAMILY, RUN_ID_FAMILY, allocate_id
from .store import utc_now
from .records import RunLoop, ThreadPeer
from .setup import AgentSetup
from .request import ExecutableKind, RunRequest
from .store import RunStore

_LOGGER = logging.getLogger("toolang.run")


@dataclass(frozen=True, slots=True)
class _Run:
    """One run bound to immutable agent state and runtime ids."""

    run_id: str
    group: str
    origin: str
    thread_id: str
    executable_kind: ExecutableKind
    executable_name: str | None
    input_text: str
    message: Message | None
    model_selector: str | None
    model_selectors: tuple[str, ...]
    tool_selectors: tuple[str, ...] | None
    cap_selectors: tuple[str, ...]
    run_loop: RunLoop
    metadata: dict[str, Any]
    state: AgentState
    setup: AgentSetup
    created_at: str


def _bind_run_request(
    request: RunRequest,
    *,
    root: Path,
    name: str,
    state: AgentState,
    setup: AgentSetup,
    store: RunStore,
) -> _Run:
    """Bind one run request to immutable runtime inputs."""

    thread_id = request.thread_id or _request_thread_id(root, name, request)
    thread_peer = _request_thread_peer(request.metadata)
    existing_thread = store.get_thread(thread_id=thread_id)
    store.ensure_thread(
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
    return _Run(
        run_id=request.run_id or allocate_run_id(root, name),
        group=request.group,
        origin=request.origin,
        thread_id=thread_id,
        executable_kind=request.executable_kind,
        executable_name=request.executable_name,
        input_text=_request_input_text(request),
        message=request.message,
        model_selector=_request_model_selector(request),
        model_selectors=tuple(request.model_selectors),
        tool_selectors=None if request.tool_selectors is None else tuple(request.tool_selectors),
        cap_selectors=tuple(request.cap_selectors),
        run_loop=run_loop,
        metadata=dict(request.metadata),
        state=state,
        setup=setup,
        created_at=utc_now(),
    )


def allocate_run_id(root: Path, name: str) -> str:
    value = allocate_id(
        agents.agent_id_state_path(root, name),
        family=RUN_ID_FAMILY,
    ).value
    return f"run_{value}"


def run_selected_model_selector(run: _Run) -> str | None:
    if isinstance(run.model_selector, str) and run.model_selector.strip():
        return run.model_selector.strip()
    for key in ("model", "model_selector"):
        value = run.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def invoke_params(run: _Run) -> dict[str, Any]:
    value = run.metadata.get("invoke_params")
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def invoke_parts(run: _Run) -> tuple[dict[str, Any], ...]:
    value = run.metadata.get("invoke_parts")
    if not isinstance(value, list):
        return ()
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        items.append({str(key): part for key, part in item.items()})
    return tuple(items)


def run_job_context(run: _Run) -> dict[str, object] | None:
    value = run.metadata.get("job")
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def allocate_thread_id(root: Path, name: str, kind: str) -> str:
    """Allocate one process-safe thread id for a caller-facing surface."""

    value = allocate_id(
        agents.agent_id_state_path(root, name),
        family=LOCAL_ID_FAMILY,
    ).value
    return f"{_thread_id_kind(kind)}_{value}"


def _request_thread_id(root: Path, name: str, request: RunRequest) -> str:
    if request.origin == "script":
        return allocate_thread_id(root, name, "script")
    return allocate_thread_id(root, name, request.thread_kind or request.origin)


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
    if request.input:
        return request.input
    if request.message is None:
        return ""
    return message_text(request.message.parts)


def _request_model_selector(request: RunRequest) -> str | None:
    if isinstance(request.model_selector, str) and request.model_selector.strip():
        return request.model_selector.strip()
    return None
