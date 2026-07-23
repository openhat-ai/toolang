"""Agent-to-agent chat tool plugin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from typing import Any

import httpx

from toolang.common.errors import ToolangError
from toolang.base.protocols.tool import AgentTool, AgentToolSet
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.tool import ToolContext
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.common.time import utc_now
from toolang.execution.store import RunStore
from toolang.execution.executor.persist import PersistSink
from toolang.execution.events import RunBegin, RunEnd, StepBegin, StepEnd
from toolang.execution.records import RunControlRef, OutputRef, ThreadPeer, ThreadRecord
from toolang.common.ids import LOCAL_ID_FAMILY, RUN_ID_FAMILY, allocate_id

DEFAULT_TIMEOUT_SEC = 60
DEFAULT_MAX_RESPONSE_CHARS = 20_000


@dataclass(frozen=True, slots=True)
class AgentPeer:
    """One configured peer agent endpoint."""

    name: str
    endpoint: str


@dataclass(slots=True)
class AgentChatPlugin:
    """Tools for sending chat messages to peer Toolang agents."""

    config: dict[str, Any]
    name: str = "agent_chat"
    description: str | None = (
        "Ask configured peer Toolang agents through their chat API."
    )
    _peers: dict[str, AgentPeer] = field(init=False, repr=False)
    _timeout_sec: float = field(init=False, repr=False)
    _max_response_chars: int = field(init=False, repr=False)
    _tools: dict[str, AgentTool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._peers = _parse_peers(self.config.get("peers"))
        self._timeout_sec = float(
            _positive_int(self.config.get("timeout_sec"), default=DEFAULT_TIMEOUT_SEC)
        )
        self._max_response_chars = _positive_int(
            self.config.get("max_response_chars"),
            default=DEFAULT_MAX_RESPONSE_CHARS,
        )
        self._tools = self._build_tools()

    def tools(self) -> Mapping[str, AgentTool]:
        return dict(self._tools)

    def _build_tools(self) -> dict[str, AgentTool]:
        @tool(
            name="peers",
            description="List configured peer agents available for agent_chat.",
        )
        def peers() -> dict[str, Any]:
            return {
                "peers": [
                    {
                        "name": peer.name,
                        "endpoint": peer.endpoint,
                    }
                    for peer in sorted(self._peers.values(), key=lambda item: item.name)
                ]
            }

        @tool(
            name="send",
            description=(
                "Send one message to a peer Toolang agent. The peer can be either one configured "
                "peer name or an object with name and endpoint. The tool creates or reuses "
                "one local child a2a thread for the current user thread and records the peer thread "
                "returned by the remote agent."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "peer": {
                        "description": (
                            "Either a configured peer name string or an object like "
                            '{"name":"bob","endpoint":"http://127.0.0.1:7002"}.'
                        ),
                        "oneOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "endpoint": {"type": "string"},
                                },
                                "required": ["name", "endpoint"],
                                "additionalProperties": False,
                            },
                        ],
                    },
                    "message": {"type": "string"},
                    "thread": {"type": "string"},
                    "stream": {
                        "type": "boolean",
                        "description": "When true, call the peer agent's streaming chat endpoint and aggregate text deltas.",
                        "default": False,
                    },
                },
                "required": ["peer", "message"],
                "additionalProperties": False,
            },
        )
        def send(
            peer: Any,
            message: str,
            thread: str | None = None,
            stream: bool = False,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            if context is None:
                raise ToolangError("agent_chat tool context is required")
            target = self._peer(peer)
            text = str(message)
            if not text.strip():
                raise ToolangError("agent_chat message cannot be empty")
            store = RunStore(context.home / ".runtime" / "runs.db")
            try:
                current_run = store.get_run(run_id=context.run_id)
                if current_run is None:
                    raise ToolangError(f"current run not found: {context.run_id}")
                local_thread = (
                    _load_local_thread(store, thread)
                    if thread is not None
                    else _find_or_create_local_thread(
                        store, context, parent=current_run.thread, peer=target.name
                    )
                )
                if local_thread.peer.name != target.name:
                    raise ToolangError(
                        f"agent_chat local thread peer mismatch: {local_thread.thread_id}"
                    )
                peer_thread = local_thread.peer.thread
                payload: dict[str, Any] = {
                    "client": "chat",
                    "peer": {
                        "type": "agent",
                        "name": context.home.name,
                        "thread": local_thread.thread_id,
                    },
                    "message": {
                        "role": "user",
                        "parts": [{"type": "text", "text": text}],
                    },
                }
                if peer_thread is not None:
                    payload["thread"] = peer_thread
                response = (
                    _stream_chat(
                        target.endpoint, payload, timeout_sec=self._timeout_sec
                    )
                    if stream
                    else _post_chat(
                        target.endpoint, payload, timeout_sec=self._timeout_sec
                    )
                )
                remote_thread = str(response.get("thread_id", "")).strip()
                if not remote_thread:
                    raise ToolangError("agent_chat response did not include thread_id")
                if local_thread.peer.thread != remote_thread:
                    local_thread = store.update_thread_peer(
                        thread_id=local_thread.thread_id,
                        peer=ThreadPeer(
                            type="agent", name=target.name, thread=remote_thread
                        ),
                    )
                full_assistant_text = _assistant_text(response)
                assistant_text = full_assistant_text[: self._max_response_chars]
                mirror_run_id = _record_local_a2a_exchange(
                    store,
                    context,
                    thread=local_thread,
                    peer=target.name,
                    message=text,
                    assistant_text=assistant_text,
                    remote_run_id=response.get("run_id"),
                )
                return {
                    "peer": target.name,
                    "local_thread": local_thread.thread_id,
                    "peer_thread": remote_thread,
                    "run_id": response.get("run_id"),
                    "local_run_id": mirror_run_id,
                    "assistant_text": assistant_text,
                    "assistant": response.get("assistant"),
                    "streamed": bool(stream),
                    "truncated": len(full_assistant_text) > self._max_response_chars,
                }
            finally:
                store.close()

        return {
            "peers": create_function_tool(peers),
            "send": create_function_tool(send),
        }

    def _peer(self, value: Any) -> AgentPeer:
        if isinstance(value, Mapping):
            payload = dict(value)
            peer_name = str(payload.get("name", "")).strip()
            endpoint = str(payload.get("endpoint", "")).strip().rstrip("/")
            if not peer_name or not endpoint:
                raise ToolangError("agent_chat peer object requires name and endpoint")
            return AgentPeer(name=peer_name, endpoint=endpoint)

        peer_name = str(value).strip()
        if not peer_name:
            raise ToolangError("agent_chat peer cannot be empty")
        peer = self._peers.get(peer_name)
        if peer is None:
            raise ToolangError(
                f"unknown agent_chat peer: {peer_name}; pass an object with name and endpoint"
            )
        return peer


def create_tool_set(config: Mapping[str, Any]) -> AgentToolSet:
    """Create the agent_chat tool plugin."""

    return AgentChatPlugin(config=dict(config))


def _parse_peers(raw: object) -> dict[str, AgentPeer]:
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ToolangError("agent_chat peers must be a list")
    peers: dict[str, AgentPeer] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ToolangError("agent_chat peer entries must be objects")
        payload = dict(item)
        name = str(payload.get("name", "")).strip()
        endpoint = str(payload.get("endpoint", "")).strip().rstrip("/")
        if not name or not endpoint:
            raise ToolangError("agent_chat peer requires name and endpoint")
        peers[name] = AgentPeer(name=name, endpoint=endpoint)
    return peers


def _positive_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = (
            value
            if isinstance(value, int) and not isinstance(value, bool)
            else int(str(value))
        )
    except (TypeError, ValueError) as exc:
        raise ToolangError("agent_chat integer config is invalid") from exc
    if parsed <= 0:
        raise ToolangError("agent_chat integer config must be positive")
    return parsed


def _load_local_thread(store: RunStore, thread_id: str | None) -> ThreadRecord:
    thread = str(thread_id or "").strip()
    if not thread:
        raise ToolangError("agent_chat thread cannot be empty")
    record = store.get_thread(thread_id=thread)
    if record is None:
        raise ToolangError(f"agent_chat local thread not found: {thread}")
    if record.peer.type != "agent":
        raise ToolangError(
            f"agent_chat local thread is not an agent peer thread: {thread}"
        )
    return record


def _find_or_create_local_thread(
    store: RunStore,
    context: ToolContext,
    *,
    parent: str,
    peer: str,
) -> ThreadRecord:
    for thread in store.list_threads():
        control = store.get_thread_control(
            thread_id=thread.thread_id, index=thread.created_by.index
        )
        if (
            control is not None
            and control.context.get("parent") == parent
            and thread.peer.type == "agent"
            and thread.peer.name == peer
        ):
            return thread
    value = allocate_id(
        context.home / ".runtime" / "ids.json", family=LOCAL_ID_FAMILY
    ).value
    thread, _control = store.create_thread(
        thread_id=f"script_{value}",
        origin="chat",
        peer=ThreadPeer(type="agent", name=peer, thread=None),
        context={"parent": parent},
    )
    return thread


def _record_local_a2a_exchange(
    store: RunStore,
    context: ToolContext,
    *,
    thread: ThreadRecord,
    peer: str,
    message: str,
    assistant_text: str,
    remote_run_id: object,
) -> str:
    run_id = f"run_{allocate_id(context.home / '.runtime' / 'ids.json', family=RUN_ID_FAMILY).value}"
    started_at = utc_now()
    thread_control = store.get_thread_control(
        thread_id=thread.thread_id, index=thread.created_by.index
    )
    input_message = Message(
        role="user",
        parts=(TextPart(text=message),),
        meta={
            "agent_chat": {
                "peer": peer,
                "remote_thread": thread.peer.thread,
                "remote_run_id": remote_run_id,
                "parent_thread": (
                    thread_control.context.get("parent")
                    if thread_control is not None
                    else None
                ),
            }
        },
    )
    run_context = {
        "root": run_id,
        "runnable": {"kind": "agent", "name": peer},
        "call": "agent_chat",
    }
    sink = PersistSink(store)
    store.accept_start(
        run_id=run_id,
        parent=None,
        thread=thread.thread_id,
        input=input_message,
        context=run_context,
        request_id=None,
        created_at=started_at,
    )
    sink.on_event(
        RunBegin(
            run=run_id,
            input=RunControlRef(index=0),
            context=run_context,
            started_at=started_at,
        )
    )
    store.finish_run_controls(run_id=run_id, indexes=(0,), finished_at=started_at)
    sink.on_event(
        StepBegin(
            step=f"{run_id}/0",
            kind="model",
            input=(RunControlRef(index=0),),
            context={"model_ref": f"agent_chat/{peer}"},
            started_at=started_at,
        )
    )
    finished_at = utc_now()
    sink.on_event(
        StepEnd(
            step=f"{run_id}/0",
            kind="model",
            status="finished",
            output=(TextPart(text=assistant_text),),
            detail={
                "model_ref": f"agent_chat/{peer}",
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "provider": "agent_chat",
                "model": peer,
                "adapter": "chat",
            },
            started_at=started_at,
            finished_at=finished_at,
        )
    )
    sink.on_event(
        RunEnd(
            run=run_id,
            status="finished",
            output=OutputRef(step=f"{run_id}/0"),
            finished_at=finished_at,
        )
    )
    return run_id


def _post_chat(
    endpoint: str, payload: dict[str, Any], *, timeout_sec: float
) -> dict[str, Any]:
    try:
        response = httpx.post(
            f"{endpoint}/api/v1/chat",
            json=payload,
            timeout=timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise ToolangError(f"agent_chat request failed: {exc}") from exc
    except ValueError as exc:
        raise ToolangError("agent_chat response was not valid JSON") from exc
    if not isinstance(data, Mapping):
        raise ToolangError("agent_chat response must be an object")
    return dict(data)


def _stream_chat(
    endpoint: str, payload: dict[str, Any], *, timeout_sec: float
) -> dict[str, Any]:
    thread_id = ""
    run_id = ""
    text_parts: list[str] = []
    try:
        with httpx.stream(
            "POST",
            f"{endpoint}/api/v1/chat/stream",
            json=payload,
            timeout=timeout_sec,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                parsed = _parse_sse_data(line)
                if parsed is None:
                    continue
                if parsed == "[DONE]":
                    break
                event = json.loads(parsed)
                if not isinstance(event, Mapping):
                    continue
                metadata = event.get("messageMetadata")
                if isinstance(metadata, Mapping):
                    thread_id = str(metadata.get("threadId") or thread_id)
                    run_id = str(metadata.get("runId") or metadata.get("id") or run_id)
                if event.get("type") == "text-delta":
                    text_parts.append(str(event.get("delta", "")))
                if event.get("type") == "error":
                    raise ToolangError(
                        str(event.get("errorText", "agent_chat stream failed"))
                    )
    except httpx.HTTPError as exc:
        raise ToolangError(f"agent_chat stream request failed: {exc}") from exc
    except ValueError as exc:
        raise ToolangError("agent_chat stream response was not valid JSON") from exc
    assistant_text = "".join(text_parts)
    return {
        "thread_id": thread_id,
        "run_id": run_id,
        "assistant": {
            "role": "assistant",
            "parts": [{"type": "text", "text": assistant_text}]
            if assistant_text
            else [],
        },
    }


def _parse_sse_data(line: str | bytes) -> str | None:
    text = line.decode("utf-8") if isinstance(line, bytes) else str(line)
    if not text.startswith("data: "):
        return None
    return text.removeprefix("data: ").strip()


def _assistant_text(response: Mapping[str, Any]) -> str:
    assistant = response.get("assistant")
    if not isinstance(assistant, Mapping):
        return ""
    try:
        message = Message.from_data(assistant)
    except ValueError:
        return ""
    return message_text(message.parts)
