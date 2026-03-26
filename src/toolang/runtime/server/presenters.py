from __future__ import annotations

import json

from toolang.agent.prepared import PreparedAgent
from toolang.bus.db import AgentSnapshot, RunSnapshot, StoredEvent
from toolang.concepts.caps import ServiceFrontmatter
from toolang.concepts.execution import RunRecord, StepRecord, ThreadRecord
from toolang.concepts.messages import part_to_dict
from toolang.caps.view import CapView, SkillCapView
from toolang.runtime.api_models import (
    AgentCapsResponse,
    AgentChatMessage,
    CapItem,
    EventItem,
    RunItem,
    RunStepItem,
    ThreadItem,
)
from toolang.runtime.chats import ChatMessage

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
        services=[service_item(item) for item in caps.services],
        counts={
            "psyches": len(caps.psyches),
            "skills": len(caps.skills),
            "services": len(caps.services),
        },
    )


def skill_item(item: SkillCapView) -> CapItem:
    return CapItem(name=item.name, source=item.ref, effective=item.path)


def service_item(item: CapView) -> CapItem:
    source = (
        item.front_matter.target
        if isinstance(item.front_matter, ServiceFrontmatter)
        else None
    )
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


def bus_run_item(item: RunSnapshot) -> RunItem:
    return RunItem(
        id=item.run_id,
        origin=item.origin,
        thread_id=item.thread_id,
        summary=item.summary,
        status=item.status,
        type=item.run_type,
        agent_id=item.agent_id,
        parent_run_id=item.parent_run_id,
        error=item.error,
        created_at=item.created_at,
        started_at=item.created_at,
        finished_at=None if item.status == "running" else item.updated_at,
        updated_at=item.updated_at,
    )


def runtime_run_item(item: RunRecord) -> RunItem:
    return RunItem(
        id=item.run_id,
        origin=item.origin,
        thread_id=item.thread_id,
        activation_id=item.activation_id,
        channel=item.channel,
        sender=item.sender,
        execution_strategy=item.execution_strategy,
        input_text=item.input_text,
        output_text=item.output_text,
        status=item.status,
        error=item.error,
        created_at=item.created_at,
        started_at=item.started_at,
        finished_at=item.finished_at,
        updated_at=item.finished_at or item.started_at,
    )


def step_item(item: StepRecord) -> RunStepItem:
    return RunStepItem(
        id=item.step_id,
        run_id=item.run_id,
        seq=item.seq,
        kind=item.step_kind,
        status=item.status,
        input=dict(item.input_json),
        output=dict(item.output_json),
        error=item.error,
        started_at=item.started_at,
        finished_at=item.finished_at,
    )


def thread_item(
    thread: ThreadRecord,
    *,
    preview: str | None = None,
    channel: str | None = None,
) -> ThreadItem:
    return ThreadItem(
        id=thread.thread_id,
        kind=thread.thread_group,
        title=thread.title,
        preview=preview,
        channel=channel,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def message_item(message: ChatMessage) -> AgentChatMessage:
    return AgentChatMessage(
        id=message.id,
        thread_id=message.thread_id,
        run_id=message.run_id,
        seq=message.seq,
        role=message.role,
        parts=[part_to_dict(item) for item in message.parts],
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
