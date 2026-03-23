"""HTTP API request and response models for agent and bus surfaces."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from toolang.concepts.persisted.prompt_trace import PromptTrace as PersistedPromptTrace
from toolang.concepts.persisted.work import TaskStatus


class RunRequest(BaseModel):
    """Request body for one stateless run invocation."""

    thunk: str | None = None
    input: str | None = None
    model: str | None = None


class RunResponse(BaseModel):
    """Response body for one completed stateless run invocation."""

    run_id: str
    output: str


class ChatRequest(BaseModel):
    """Request body for one chat turn submission."""

    thread: str
    message: str
    thunk: str | None = None
    model: str | None = None


class AgentChatMessage(BaseModel):
    """Stored chat message returned by the runtime API."""

    id: int
    thread_id: str
    turn_id: str
    seq: int
    role: str
    parts: list[dict[str, Any]]
    created_at: str
    meta: dict[str, Any]


class ChatResponse(BaseModel):
    """Response body for one completed chat turn."""

    thread_id: str
    turn_id: str
    message: AgentChatMessage
    assistant: AgentChatMessage


class AgentProfile(BaseModel):
    """Public profile metadata for one running agent."""

    agent: str
    display_name: str | None = None
    title: str | None = None
    summary: str | None = None
    description: str | None = None
    avatar: str | None = None


class AgentRuntimeResponse(BaseModel):
    """Runtime status payload for one running agent."""

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
    """One capability entry shown by the runtime API."""

    name: str
    source: str | None = None
    effective: str | None = None


class ChoreItem(BaseModel):
    """One local chore entry shown by the runtime API."""

    id: str
    title: str | None = None
    thread_id: str
    interval_sec: int
    thunk: str | None = None
    model: str | None = None
    path: str
    last_enqueued_at: str | None = None
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_status: str | None = None
    last_run_id: str | None = None
    next_due_at: str | None = None
    updated_at: str | None = None
    paused: bool | None = None


class TaskItem(BaseModel):
    """One local task entry shown by the runtime API."""

    id: str
    title: str | None = None
    status: str
    assignee: str | None = None
    thread_id: str
    thunk: str | None = None
    model: str | None = None
    path: str
    last_enqueued_at: str | None = None
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_status: str | None = None
    last_run_id: str | None = None
    updated_at: str | None = None
    paused: bool | None = None


class TaskPutRequest(BaseModel):
    """Full task document written through the runtime API."""

    title: str | None = None
    body: str = ""
    status: TaskStatus = "open"
    assignee: str | None = None
    thread_id: str | None = None
    thunk: str | None = None
    model: str | None = None
    paused: bool = False


class TaskPatchRequest(BaseModel):
    """Partial task document update written through the runtime API."""

    title: str | None = None
    body: str | None = None
    body_append: str | None = None
    status: TaskStatus | None = None
    assignee: str | None = None
    thread_id: str | None = None
    thunk: str | None = None
    model: str | None = None
    paused: bool | None = None


class ChorePutRequest(BaseModel):
    """Full chore document written through the runtime API."""

    title: str | None = None
    body: str = ""
    thread_id: str | None = None
    interval_sec: int = Field(default=300, ge=1)
    thunk: str | None = None
    model: str | None = None
    paused: bool = False


class ChorePatchRequest(BaseModel):
    """Partial chore document update written through the runtime API."""

    title: str | None = None
    body: str | None = None
    body_append: str | None = None
    thread_id: str | None = None
    interval_sec: int | None = Field(default=None, ge=1)
    thunk: str | None = None
    model: str | None = None
    paused: bool | None = None


class WillItem(BaseModel):
    """The local will document shown by the runtime API."""

    title: str | None = None
    thread_id: str
    interval_sec: int
    thunk: str | None = None
    model: str | None = None
    path: str
    last_enqueued_at: str | None = None
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_status: str | None = None
    last_run_id: str | None = None
    next_due_at: str | None = None
    updated_at: str | None = None
    paused: bool | None = None


class WillPutRequest(BaseModel):
    """Full will document written through the runtime API."""

    title: str | None = None
    body: str = ""
    thread_id: str | None = None
    interval_sec: int = Field(default=300, ge=1)
    thunk: str | None = None
    model: str | None = None
    paused: bool = False


class WillPatchRequest(BaseModel):
    """Partial will document update written through the runtime API."""

    title: str | None = None
    body: str | None = None
    body_append: str | None = None
    thread_id: str | None = None
    interval_sec: int | None = Field(default=None, ge=1)
    thunk: str | None = None
    model: str | None = None
    paused: bool | None = None


class AgentCapsResponse(BaseModel):
    """Capability summary returned by the runtime API."""

    agent: str
    psyches: list[CapItem] = Field(default_factory=list)
    skills: list[CapItem] = Field(default_factory=list)
    servers: list[CapItem] = Field(default_factory=list)
    chores: list[ChoreItem] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class ChatThreadItem(BaseModel):
    """One chat thread listed by the runtime API."""

    id: str
    agent: str
    title: str | None = None
    created_at: str
    updated_at: str


class ChatThreadListResponse(BaseModel):
    """Collection response for chat thread listings."""

    items: list[ChatThreadItem]


class TaskListResponse(BaseModel):
    """Collection response for local task listings."""

    items: list[TaskItem]


class ChoreListResponse(BaseModel):
    """Collection response for local chore listings."""

    items: list[ChoreItem]


class WillResponse(BaseModel):
    """Response containing the local will document, if present."""

    item: WillItem | None = None


class PromptTraceItem(PersistedPromptTrace):
    """Prompt trace payload returned by the runtime API."""


class ChatTurnItem(BaseModel):
    """One stored turn within a chat thread."""

    thread_id: str
    turn_id: str
    messages: list[AgentChatMessage]
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None


class ChatThreadResponse(BaseModel):
    """Detailed chat thread response with stored turns."""

    thread: ChatThreadItem
    turns: list[ChatTurnItem]


class EventItem(BaseModel):
    """One event entry returned by the runtime or bus API."""

    event_id: int
    event_type: str
    at: str
    agent_id: str
    run_id: str | None = None
    payload: dict[str, Any]


class EventListResponse(BaseModel):
    """Collection response for event listings."""

    items: list[EventItem]


class SchedulerThreadGroupDiagnostics(BaseModel):
    """Concurrency diagnostics for one scheduler thread group."""

    kind: str
    limit: int
    in_flight: int
    available: int


class SchedulerDiagnostics(BaseModel):
    """Runtime scheduler diagnostics."""

    max_workers: int
    tracked_threads: int
    thread_groups: list[SchedulerThreadGroupDiagnostics] = Field(default_factory=list)


class ChannelDiagnostics(BaseModel):
    """Runtime diagnostics for one configured channel binding."""

    name: str
    plugin: str
    ok: bool | None = None
    detail: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    poll_state_path: str | None = None
    poll_cursor: str | None = None
    poll_meta: dict[str, Any] = Field(default_factory=dict)


class HookDiagnostics(BaseModel):
    """Runtime diagnostics for one configured hook binding."""

    name: str
    path: str
    method: str
    plugin: str


class PulseDiagnostics(BaseModel):
    """Runtime diagnostics for pulse submissions."""

    state_path: str
    pending: list[str] = Field(default_factory=list)


class RuntimeDiagnosticsResponse(BaseModel):
    """Operational diagnostics returned by the runtime API."""

    runtime_loops: list[str] = Field(default_factory=list)
    hook_loop_enabled: bool = False
    scheduler: SchedulerDiagnostics
    channels: list[ChannelDiagnostics] = Field(default_factory=list)
    hooks: list[HookDiagnostics] = Field(default_factory=list)
    pulse: PulseDiagnostics | None = None


class RunItem(BaseModel):
    """One run summary entry returned by the runtime or bus API."""

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
    """Collection response for run listings."""

    items: list[RunItem]


class RunDetailResponse(BaseModel):
    """Detailed run response with children, events, and optional turn state."""

    run: RunItem
    children: list[RunItem]
    events: list[EventItem]
    turn: ChatTurnItem | None = None


class BusAgentItem(BaseModel):
    """One agent summary entry returned by the bus API."""

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
    """Collection response for bus agent listings."""

    items: list[BusAgentItem]
