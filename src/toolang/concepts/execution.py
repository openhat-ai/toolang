"""Runtime execution concepts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .messages import MessagePart, MessageRole

MessageOrigin = Literal["invoke", "chat", "task", "chore", "will"]
MessageSender = Literal["owner", "peer", "guest", "self", "service"]
RuntimeLoop = Literal["server", "poll", "hook", "pulse"]
ExecutionStrategy = Literal["direct", "react"]
ActivationKind = Literal["runtime", "invoke"]
ActivationStatus = Literal["running", "finished", "failed", "stopped"]
ThreadGroup = Literal["invoke", "chat", "task", "chore", "will"]
RunStatus = Literal["running", "finished", "failed"]
StepKind = Literal["prompt_build", "model_call", "tool_call", "delivery"]
StepStatus = Literal["finished", "failed"]


@dataclass(frozen=True, slots=True)
class Message:
    """One normalized runtime input message."""

    origin: MessageOrigin
    channel: str | None
    sender: MessageSender
    thread_id: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActivationRecord:
    """One persisted activation record in the execution truth layer."""

    activation_id: str
    agent_uri: str
    agent_id: str
    agent_name: str
    activation_kind: ActivationKind
    status: ActivationStatus
    started_at: str
    finished_at: str | None
    runtime_loops: tuple[RuntimeLoop, ...]
    sandbox: str
    cap_scopes: tuple[str, ...]
    sync_fingerprint: str | None = None
    plugin_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ThreadRecord:
    """One persisted execution thread."""

    thread_id: str
    agent_uri: str
    thread_group: ThreadGroup
    title: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One persisted run within a thread and activation."""

    run_id: str
    activation_id: str
    thread_id: str
    origin: MessageOrigin
    channel: str | None
    sender: MessageSender
    execution_strategy: ExecutionStrategy
    status: RunStatus
    input_text: str | None
    output_text: str | None
    error: str | None
    created_at: str
    started_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One persisted step within a run."""

    step_id: int
    run_id: str
    seq: int
    step_kind: StepKind
    status: StepStatus
    input_json: dict[str, Any]
    output_json: dict[str, Any]
    error: str | None
    started_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class RunMessageRecord:
    """One persisted transcript message within a thread and run."""

    id: str
    thread_id: str
    run_id: str
    seq: int
    role: MessageRole
    origin: MessageOrigin
    channel: str | None
    sender: MessageSender
    text: str
    created_at: str
    meta: dict[str, Any] = field(default_factory=dict)
    parts: tuple[MessagePart, ...] = ()


def thread_group_for_origin(origin: MessageOrigin) -> ThreadGroup:
    """Return the default scheduling group for one message origin."""

    return origin
