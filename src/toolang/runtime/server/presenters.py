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
    CapDetailItem,
    CapDetailResponse,
    CapMutationItem,
    CapMutationResponse,
    CapItem,
    CapListResponse,
    EventItem,
    RunItem,
    RunStepItem,
    ThreadItem,
)
from toolang.runtime.chats import ChatMessage
from toolang.runtime.cap_defs import CapMutationResult

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


def caps_response(prepared: PreparedAgent, caps) -> AgentCapsResponse:
    return AgentCapsResponse(
        agent=prepared.ref.name,
        psyches=[psyche_item(item, agent_kind=prepared.ref.kind) for item in caps.psyches],
        prompts=[prompt_item(item, agent_kind=prepared.ref.kind) for item in caps.prompts],
        skills=[skill_item(item, agent_kind=prepared.ref.kind) for item in caps.skills],
        services=[service_item(item, agent_kind=prepared.ref.kind) for item in caps.services],
        counts={
            "psyches": len(caps.psyches),
            "prompts": len(caps.prompts),
            "skills": len(caps.skills),
            "services": len(caps.services),
        },
    )


def cap_mutation_response(result: CapMutationResult) -> CapMutationResponse:
    return CapMutationResponse(
        item=CapMutationItem(
            kind=result.kind,
            name=result.name,
            scope=result.scope,
            source=result.source,
            locator=result.locator,
            path=result.path,
            ref=result.ref,
        )
    )


def cap_list_response(items: list[CapItem]) -> CapListResponse:
    return CapListResponse(items=items)


def cap_detail_response(item: CapDetailItem) -> CapDetailResponse:
    return CapDetailResponse(item=item)


def skill_item(item: SkillCapView, *, agent_kind: str) -> CapItem:
    return _cap_list_item(
        kind=item.kind,
        name=item.name,
        scope=item.scope,
        agent_kind=agent_kind,
        source=item.ref or item.source_path or item.path,
        effective=item.path,
        path=item.source_path,
        ref=item.ref,
        description=_front_matter_description(item.front_matter),
    )


def skill_detail_item(item: SkillCapView, *, agent_kind: str) -> CapDetailItem:
    return CapDetailItem(
        **skill_item(item, agent_kind=agent_kind).model_dump(mode="python"),
        content=item.content,
        entry_path=item.entry_path,
        files=list(item.files),
    )


def service_item(item: CapView, *, agent_kind: str) -> CapItem:
    source = (
        item.front_matter.target
        if isinstance(item.front_matter, ServiceFrontmatter)
        else item.ref or item.source_path or item.path
    )
    return _cap_list_item(
        kind=item.kind,
        name=item.name,
        scope=item.scope,
        agent_kind=agent_kind,
        source=source,
        effective=item.path,
        path=item.source_path if item.ref is None else None,
        ref=item.ref,
        description=_front_matter_description(item.front_matter),
        params=list(item.params),
    )


def service_detail_item(item: CapView, *, agent_kind: str) -> CapDetailItem:
    return CapDetailItem(
        **service_item(item, agent_kind=agent_kind).model_dump(mode="python"),
        content=item.content,
    )


def prompt_item(item: CapView, *, agent_kind: str) -> CapItem:
    return _cap_list_item(
        kind=item.kind,
        name=item.name,
        scope=item.scope,
        agent_kind=agent_kind,
        source=item.ref or item.source_path or item.path,
        effective=item.path,
        path=item.source_path if item.ref is None else None,
        ref=item.ref,
        description=_front_matter_description(item.front_matter),
        params=list(item.params),
    )


def prompt_detail_item(item: CapView, *, agent_kind: str) -> CapDetailItem:
    return CapDetailItem(
        **prompt_item(item, agent_kind=agent_kind).model_dump(mode="python"),
        content=item.content,
    )


def psyche_item(item: CapView, *, agent_kind: str) -> CapItem:
    return _cap_list_item(
        kind=item.kind,
        name=item.name,
        scope=item.scope,
        agent_kind=agent_kind,
        source=item.ref or item.source_path or item.path,
        effective=item.path,
        path=item.source_path if item.ref is None else None,
        ref=item.ref,
        description=_front_matter_description(item.front_matter),
        params=list(item.params),
    )


def psyche_detail_item(item: CapView, *, agent_kind: str) -> CapDetailItem:
    return CapDetailItem(
        **psyche_item(item, agent_kind=agent_kind).model_dump(mode="python"),
        content=item.content,
    )


def _cap_list_item(
    *,
    kind: str,
    name: str,
    scope: str,
    agent_kind: str,
    source: str | None,
    effective: str,
    path: str | None,
    ref: str | None,
    description: str | None,
    params: list[dict[str, object]] | None = None,
) -> CapItem:
    return CapItem(
        kind=kind,
        name=name,
        scope=scope,
        editable=_cap_editable(agent_kind=agent_kind, ref=ref, source_path=path),
        source=string_or_none(source),
        effective=effective,
        path=string_or_none(path),
        ref=ref,
        description=description,
        params=list(params or []),
    )


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


def _cap_editable(*, agent_kind: str, ref: str | None, source_path: str | None) -> bool:
    return agent_kind != "visiting" and bool(ref or source_path)


def _front_matter_description(front_matter: object) -> str | None:
    return string_or_none(getattr(front_matter, "description", None))


def sse(event: str, data: dict[str, object], event_id: int | None = None) -> str:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def data_sse(chunk: dict[str, object]) -> str:
    return "data: " + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\n\n"
