"""Agent-to-agent chat tool plugin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from toolang.base.error import ToolangError
from toolang.base.protocols.tool import Tool, ToolPlugin
from toolang.base.types.message import Message, message_text
from toolang.base.types.tool import ToolContext
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.execution.db import ExecutionStore
from toolang.execution.records import ThreadPeer, ThreadRecord
from toolang.ids import LOCAL_ID_FAMILY, allocate_id

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
    description: str | None = "Ask configured peer Toolang agents through their chat API."
    _peers: dict[str, AgentPeer] = field(init=False, repr=False)
    _timeout_sec: float = field(init=False, repr=False)
    _max_response_chars: int = field(init=False, repr=False)
    _tools: dict[str, Tool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._peers = _parse_peers(self.config.get("peers"))
        self._timeout_sec = float(_positive_int(self.config.get("timeout_sec"), default=DEFAULT_TIMEOUT_SEC))
        self._max_response_chars = _positive_int(
            self.config.get("max_response_chars"),
            default=DEFAULT_MAX_RESPONSE_CHARS,
        )
        self._tools = self._build_tools()

    def tools(self) -> Mapping[str, Tool]:
        return dict(self._tools)

    def _build_tools(self) -> dict[str, Tool]:
        @tool(name="peers", description="List configured peer agents available for agent_chat.")
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
                            "{\"name\":\"bob\",\"endpoint\":\"http://127.0.0.1:7002\"}."
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
                },
                "required": ["peer", "message"],
                "additionalProperties": False,
            },
        )
        def send(
            peer: Any,
            message: str,
            thread: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            if context is None:
                raise ToolangError("agent_chat tool context is required")
            target = self._peer(peer)
            text = str(message)
            if not text.strip():
                raise ToolangError("agent_chat message cannot be empty")
            store = ExecutionStore(context.home / ".runtime" / "execution.db")
            try:
                current_run = store.get_run(run_id=context.run_id)
                if current_run is None:
                    raise ToolangError(f"current run not found: {context.run_id}")
                local_thread = (
                    _load_local_thread(store, thread)
                    if thread is not None
                    else _find_or_create_local_thread(store, context, parent=current_run.thread_id, peer=target.name)
                )
                if local_thread.peer.name != target.name:
                    raise ToolangError(
                        f"agent_chat local thread peer mismatch: {local_thread.thread_id}"
                    )
                peer_thread = local_thread.peer.thread
                payload: dict[str, Any] = {
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
                response = _post_chat(
                    target.endpoint,
                    payload,
                    timeout_sec=self._timeout_sec,
                )
                remote_thread = str(response.get("thread_id", "")).strip()
                if not remote_thread:
                    raise ToolangError("agent_chat response did not include thread_id")
                if local_thread.peer.thread != remote_thread:
                    local_thread = store.update_thread_peer(
                        thread_id=local_thread.thread_id,
                        peer=ThreadPeer(type="agent", name=target.name, thread=remote_thread),
                    )
                assistant_text = _assistant_text(response)[: self._max_response_chars]
                return {
                    "peer": target.name,
                    "local_thread": local_thread.thread_id,
                    "peer_thread": remote_thread,
                    "run_id": response.get("run_id"),
                    "assistant_text": assistant_text,
                    "assistant": response.get("assistant"),
                    "truncated": len(_assistant_text(response)) > self._max_response_chars,
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
            raise ToolangError(f"unknown agent_chat peer: {peer_name}; pass an object with name and endpoint")
        return peer


def create_tool(config: Mapping[str, Any]) -> ToolPlugin:
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
        parsed = value if isinstance(value, int) and not isinstance(value, bool) else int(str(value))
    except (TypeError, ValueError) as exc:
        raise ToolangError("agent_chat integer config is invalid") from exc
    if parsed <= 0:
        raise ToolangError("agent_chat integer config must be positive")
    return parsed


def _load_local_thread(store: ExecutionStore, thread_id: str | None) -> ThreadRecord:
    thread = str(thread_id or "").strip()
    if not thread:
        raise ToolangError("agent_chat thread cannot be empty")
    record = store.get_thread(thread_id=thread)
    if record is None:
        raise ToolangError(f"agent_chat local thread not found: {thread}")
    if record.peer.type != "agent":
        raise ToolangError(f"agent_chat local thread is not an agent peer thread: {thread}")
    return record


def _find_or_create_local_thread(
    store: ExecutionStore,
    context: ToolContext,
    *,
    parent: str,
    peer: str,
) -> ThreadRecord:
    for thread in store.list_threads():
        if thread.parent == parent and thread.peer.type == "agent" and thread.peer.name == peer:
            return thread
    value = allocate_id(context.home / ".runtime" / "ids.json", family=LOCAL_ID_FAMILY).value
    return store.ensure_thread(
        thread_id=f"chat_{value}",
        origin="chat",
        peer=ThreadPeer(type="agent", name=peer, thread=None),
        parent=parent,
    )


def _post_chat(endpoint: str, payload: dict[str, Any], *, timeout_sec: float) -> dict[str, Any]:
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


def _assistant_text(response: Mapping[str, Any]) -> str:
    assistant = response.get("assistant")
    if not isinstance(assistant, Mapping):
        return ""
    try:
        message = Message.from_data(assistant)
    except ValueError:
        return ""
    return message_text(message.parts)
