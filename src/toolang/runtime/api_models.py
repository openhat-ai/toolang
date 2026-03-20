from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    thunk: str | None = None
    input: str | None = None
    model: str | None = None


class RunResponse(BaseModel):
    run_id: str
    output: str


class ChatRequest(BaseModel):
    thread: str
    message: str
    thunk: str | None = None
    model: str | None = None


class AgentChatMessage(BaseModel):
    id: int
    thread_id: str
    turn_id: str
    seq: int
    role: str
    parts: list[dict[str, Any]]
    created_at: str
    meta: dict[str, Any]


class ChatResponse(BaseModel):
    thread_id: str
    turn_id: str
    message: AgentChatMessage
    assistant: AgentChatMessage


class AgentProfile(BaseModel):
    agent: str
    display_name: str | None = None
    title: str | None = None
    summary: str | None = None
    description: str | None = None
    avatar: str | None = None


class AgentRuntimeResponse(BaseModel):
    status: str
    checked_at: str
    endpoint: str | None = None
    execution_host: str
    working_directory: str
    sandbox: str
    network: str
    approvals: str
    filesystem_scope: str
    os: str
    arch: str
    runtime: str
    runtime_version: str
    started_at: str | None = None
    model: str | None = None


class CapItem(BaseModel):
    name: str
    source: str | None = None
    effective: str | None = None


class ChoreItem(BaseModel):
    name: str
    created_at: str | None = None
    updated_at: str | None = None
    compiled_at: str | None = None
    needs_recompile: bool | None = None
    paused: bool | None = None


class AgentCapsResponse(BaseModel):
    agent: str
    psyches: list[CapItem] = Field(default_factory=list)
    skills: list[CapItem] = Field(default_factory=list)
    servers: list[CapItem] = Field(default_factory=list)
    chores: list[ChoreItem] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class ChatThreadItem(BaseModel):
    id: str
    agent: str
    title: str | None = None
    created_at: str
    updated_at: str


class ChatThreadListResponse(BaseModel):
    items: list[ChatThreadItem]


class ChatTurnItem(BaseModel):
    thread_id: str
    turn_id: str
    messages: list[AgentChatMessage]
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None


class ChatThreadResponse(BaseModel):
    thread: ChatThreadItem
    turns: list[ChatTurnItem]


class EventItem(BaseModel):
    event_id: int
    event_type: str
    at: str
    agent_id: str
    run_id: str | None = None
    payload: dict[str, Any]


class EventListResponse(BaseModel):
    items: list[EventItem]


class RunItem(BaseModel):
    id: str
    summary: str | None = None
    status: str
    type: str
    agent_id: str
    parent_run_id: str | None = None
    error: str | None = None
    thread_id: str | None = None
    origin_kind: str | None = None
    origin_actor: str | None = None
    origin_subject: str | None = None
    display_title: str | None = None
    display_subtitle: str | None = None
    created_at: str
    updated_at: str


class RunListResponse(BaseModel):
    items: list[RunItem]


class RunDetailResponse(BaseModel):
    run: RunItem
    children: list[RunItem]
    events: list[EventItem]
    turn: ChatTurnItem | None = None


class BusAgentItem(BaseModel):
    id: str
    name: str
    status: str
    endpoint: str | None = None
    model: str | None = None
    host: str | None = None
    port: int | None = None
    sandbox: str | None = None
    runtime_ref: str | None = None
    detail: str | None = None
    created_at: str
    updated_at: str


class AgentListResponse(BaseModel):
    items: list[BusAgentItem]
