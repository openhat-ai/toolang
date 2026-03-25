from __future__ import annotations

import json

from toolang.agent.prepared import PreparedAgent
from toolang.bus.db import AgentSnapshot, RunSnapshot, StoredEvent
from toolang.concepts.caps import ServiceFrontmatter
from toolang.caps.view import CapView, SkillCapView
from toolang.runtime.api_models import (
    AgentCapsResponse,
    AgentChatMessage,
    CapItem,
    ChatThreadItem,
    ChatTurnItem,
    EventItem,
    RunItem,
)
from toolang.runtime.chats import ChatMessage, ChatThread, ChatTurn

SHORT_AGENT_ID_LENGTH = 12


def fallback_agent_snapshot(
    prepared: PreparedAgent,
    *,
    endpoint: str,
    sandbox: str,
    now: str,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_uri=prepared.ref.uri,
        agent_id=prepared.ref.id[:SHORT_AGENT_ID_LENGTH],
        name=prepared.ref.name,
        kind=prepared.ref.kind,
        status="prepared",
        endpoint=endpoint,
        sandbox=sandbox,
        agent_home=str(prepared.ref.home),
        source_file=prepared.ref.source.name,
        detail=None,
        created_at=now,
        updated_at=now,
    )


def caps_response(agent_name: str, caps) -> AgentCapsResponse:
    return AgentCapsResponse(
        agent=agent_name,
        psyches=[psyche_item(item) for item in caps.psyches],
        skills=[skill_item(item) for item in caps.skills],
        servers=[service_item(item) for item in caps.services],
        chores=[],
        counts={
            "psyches": len(caps.psyches),
            "skills": len(caps.skills),
            "servers": len(caps.services),
            "chores": 0,
        },
    )


def skill_item(item: SkillCapView) -> CapItem:
    return CapItem(name=item.name, source=item.ref, effective=item.path)


def service_item(item: CapView) -> CapItem:
    source = item.front_matter.target if isinstance(item.front_matter, ServiceFrontmatter) else None
    return CapItem(name=item.name, source=string_or_none(source), effective=item.path)


def psyche_item(item: CapView) -> CapItem:
    return CapItem(name=item.name, source=item.path, effective=item.path)


def event_item(item: StoredEvent) -> EventItem:
    return EventItem(
        event_id=item.event_id,
        event_type=item.event_type,
        at=item.at,
        agent_id=item.agent_id,
        run_id=item.run_id,
        payload=dict(item.payload),
    )


def run_item(item: RunSnapshot) -> RunItem:
    return RunItem(
        id=item.run_id,
        summary=item.summary,
        status=item.status,
        type=item.run_type,
        agent_id=item.agent_id,
        parent_run_id=item.parent_run_id,
        error=item.error,
        thread_id=item.thread_id,
        origin_kind=origin_kind(item.origin),
        origin_actor=origin_actor(item.origin),
        origin_subject=item.thread_id,
        display_title=item.summary,
        display_subtitle=run_display_subtitle(item),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def run_display_subtitle(item: RunSnapshot) -> str | None:
    if item.thread_id:
        return f"{item.origin} · {item.thread_id}"
    return item.origin


def origin_kind(origin: str) -> str:
    if origin in {"invoke", "chat"}:
        return "direct"
    return origin


def origin_actor(origin: str) -> str:
    if origin in {"invoke", "chat"}:
        return "owner"
    return "self"


def thread_item(thread: ChatThread) -> ChatThreadItem:
    return ChatThreadItem(
        id=thread.id,
        agent=thread.agent_name,
        title=thread.title,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def turn_item(
    turn: ChatTurn,
    *,
    tool_calls: list[dict[str, object]] | None = None,
) -> ChatTurnItem:
    return ChatTurnItem(
        thread_id=turn.thread_id,
        turn_id=turn.turn_id,
        messages=[message_item(message) for message in turn.messages],
        tool_calls=tool_calls or [],
        started_at=turn.started_at,
        finished_at=turn.finished_at,
    )


def message_item(message: ChatMessage) -> AgentChatMessage:
    return AgentChatMessage(
        id=message.id,
        thread_id=message.thread_id,
        turn_id=message.turn_id,
        seq=message.seq,
        role=message.role,
        parts=[{"type": "text", "text": message.text}],
        created_at=message.created_at,
        meta=dict(message.meta),
    )


def string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def sse(event: str, data: dict[str, object], event_id: int | None = None) -> str:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def data_sse(chunk: dict[str, object]) -> str:
    return "data: " + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\n\n"
