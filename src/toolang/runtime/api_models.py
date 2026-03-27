"""HTTP API request and response models for agent and bus surfaces."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from toolang.concepts.persisted.work import DEFAULT_SCHEDULE_RRULE
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
    """Request body for one chat run submission."""

    thread: str
    message: str
    thunk: str | None = None
    model: str | None = None


class AgentChatMessage(BaseModel):
    """Stored chat message returned by the runtime API."""

    id: str
    thread_id: str
    run_id: str
    seq: int
    role: str
    parts: list[dict[str, Any]]
    created_at: str
    meta: dict[str, Any]


class ChatResponse(BaseModel):
    """Response body for one completed chat run."""

    thread_id: str
    run_id: str
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


class SandboxSecurityInfo(BaseModel):
    """Structured sandbox signals used by the WebUI security view."""

    image: str | None = None
    volumes: list[str] = Field(default_factory=list)
    network_mode: str
    bridge: str | None = None
    dns: list[str] = Field(default_factory=list)
    host_reachability: bool


class ToolSecurityInfo(BaseModel):
    """Structured tool-availability signals used by the WebUI security view."""

    filesystem: bool
    shell: bool
    browser_use: bool
    computer_use: bool
    service_use: bool
    web_search: bool
    mem_search: bool
    file_search: bool


class AutonomySecurityInfo(BaseModel):
    """Structured autonomy signals used by the WebUI security view."""

    chores_enabled: bool
    tasks_enabled: bool
    will_enabled: bool
    will_path_exists: bool


class SelfModificationSecurityInfo(BaseModel):
    """Structured self-modification signals used by the WebUI security view."""

    can_add_caps: bool
    can_edit_will: bool
    can_write_source: bool
    can_persist_changes: bool


class RuntimeSecurityResponse(BaseModel):
    """Security-oriented runtime capability snapshot."""

    sandbox: SandboxSecurityInfo
    tools: ToolSecurityInfo
    autonomy: AutonomySecurityInfo
    self_modification: SelfModificationSecurityInfo


class AgentRuntimeResponse(BaseModel):
    """Runtime status payload for one running agent."""

    status: str
    checked_at: str
    activation_id: str | None = None
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
    security: RuntimeSecurityResponse


class CapItem(BaseModel):
    """One capability entry shown by the runtime API."""

    name: str
    source: str | None = None
    effective: str | None = None


class ChoreItem(BaseModel):
    """One local chore entry shown by the runtime API."""

    id: str
    title: str | None = None
    rrule: str
    paused: bool | None = None


class TaskItem(BaseModel):
    """One local task entry shown by the runtime API."""

    id: str
    name: str
    body: str
    status: str
    requester: str | None = None
    mirrored: bool = False
    provider: str | None = None
    remote_ref: str | None = None
    thread_id: str
    path: str
    updated_at: str | None = None
    paused: bool | None = None


class TaskPutRequest(BaseModel):
    """Full task document written through the runtime API."""

    id: str | None = None
    body: str = ""
    status: TaskStatus = "todo"
    requester: str | None = None
    paused: bool = False


class TaskPatchRequest(BaseModel):
    """Partial task document update written through the runtime API."""

    body: str | None = None
    body_append: str | None = None
    status: TaskStatus | None = None
    requester: str | None = None
    paused: bool | None = None


class ChorePutRequest(BaseModel):
    """Full chore document written through the runtime API."""

    title: str | None = None
    body: str = ""
    rrule: str = DEFAULT_SCHEDULE_RRULE
    paused: bool = False


class ChorePatchRequest(BaseModel):
    """Partial chore document update written through the runtime API."""

    title: str | None = None
    body: str | None = None
    body_append: str | None = None
    rrule: str | None = None
    paused: bool | None = None


class WillItem(BaseModel):
    """The local will document shown by the runtime API."""

    id: str
    title: str | None = None
    rrule: str
    paused: bool | None = None


class WillPutRequest(BaseModel):
    """Full will document written through the runtime API."""

    title: str | None = None
    body: str = ""
    rrule: str = DEFAULT_SCHEDULE_RRULE
    paused: bool = False


class WillPatchRequest(BaseModel):
    """Partial will document update written through the runtime API."""

    title: str | None = None
    body: str | None = None
    body_append: str | None = None
    rrule: str | None = None
    paused: bool | None = None


class AgentCapsResponse(BaseModel):
    """Capability summary returned by the runtime API."""

    agent: str
    psyches: list[CapItem] = Field(default_factory=list)
    prompts: list[CapItem] = Field(default_factory=list)
    skills: list[CapItem] = Field(default_factory=list)
    services: list[CapItem] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class ThreadItem(BaseModel):
    """One thread listed by the runtime API."""

    id: str
    kind: str
    title: str | None = None
    preview: str | None = None
    channel: str | None = None
    created_at: str
    updated_at: str


class ThreadListResponse(BaseModel):
    """Collection response for thread listings."""

    items: list[ThreadItem]


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


class ThreadResponse(BaseModel):
    """Detailed thread response with related runs and ordered messages."""

    thread: ThreadItem
    runs: list["RunItem"] = Field(default_factory=list)
    messages: list[AgentChatMessage]


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
    security: RuntimeSecurityResponse
    scheduler: SchedulerDiagnostics
    channels: list[ChannelDiagnostics] = Field(default_factory=list)
    hooks: list[HookDiagnostics] = Field(default_factory=list)
    pulse: PulseDiagnostics | None = None


class RunItem(BaseModel):
    """One run entry returned by the runtime or bus API."""

    id: str
    origin: str
    thread_id: str | None = None
    activation_id: str | None = None
    channel: str | None = None
    sender: str | None = None
    execution_strategy: str | None = None
    input_text: str | None = None
    output_text: str | None = None
    summary: str | None = None
    status: str
    type: str | None = None
    agent_id: str | None = None
    parent_run_id: str | None = None
    error: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str | None = None


class RunStepItem(BaseModel):
    """One step recorded for a run."""

    id: int
    run_id: str
    seq: int
    kind: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any]
    error: str | None = None
    started_at: str
    finished_at: str | None = None


class RunListResponse(BaseModel):
    """Collection response for run listings."""

    items: list[RunItem]


class RunDetailResponse(BaseModel):
    """Detailed run response with steps, events, and optional messages."""

    run: RunItem
    steps: list[RunStepItem]
    events: list[EventItem]
    messages: list[AgentChatMessage] = Field(default_factory=list)


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


ThreadResponse.model_rebuild()
