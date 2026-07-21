"""Caller-facing execution protocol schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from toolang.base.types.message import AudioFormat, ImageDetail, MessageRole
from .types import (
    CommandApply,
    CommandKind,
    CommandStatus,
    RunStatus,
    StepKind,
    StepPath,
    StepStatus,
    ThreadPeerType,
)


@dataclass(frozen=True, slots=True)
class TextPartData:
    """One caller-facing text part."""

    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ImagePartData:
    """One caller-facing image part."""

    image_url: str | None = None
    file_id: str | None = None
    detail: ImageDetail = "auto"
    filename: str | None = None
    media_type: str | None = None
    type: Literal["image"] = "image"


@dataclass(frozen=True, slots=True)
class AudioPartData:
    """One caller-facing audio part."""

    data: str = ""
    format: AudioFormat = "mp3"
    filename: str | None = None
    media_type: str | None = None
    type: Literal["audio"] = "audio"


@dataclass(frozen=True, slots=True)
class FilePartData:
    """One caller-facing file part."""

    file_data: str | None = None
    file_url: str | None = None
    file_id: str | None = None
    filename: str | None = None
    media_type: str | None = None
    type: Literal["file"] = "file"


@dataclass(frozen=True, slots=True)
class ToolCallPartData:
    """One caller-facing tool-call part."""

    tool_call_id: str
    tool_name: str
    tool_family: str
    input: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    type: Literal["tool_call"] = "tool_call"


@dataclass(frozen=True, slots=True)
class ToolResultPartData:
    """One caller-facing tool-result part."""

    tool_call_id: str
    tool_name: str
    tool_family: str
    output: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    type: Literal["tool_result"] = "tool_result"


MessagePartData = (
    TextPartData
    | ImagePartData
    | AudioPartData
    | FilePartData
    | ToolCallPartData
    | ToolResultPartData
)


@dataclass(frozen=True, slots=True)
class MessagePayload:
    """One caller-facing message payload without runtime identity."""

    role: MessageRole
    parts: list[MessagePartData]
    meta: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MessageData(MessagePayload):
    """One full caller-facing message schema."""

    id: str
    thread_id: str
    run_id: str
    step_index: int
    created_at: str


@dataclass(frozen=True, slots=True)
class InputRefData:
    """One caller-facing run-command input reference."""

    cmd: int = 0
    part: int | None = None


@dataclass(frozen=True, slots=True)
class OutputRefData:
    """One caller-facing step-output reference."""

    step: StepPath
    part: int | None = None


StepInputData = InputRefData | OutputRefData | MessagePayload


@dataclass(frozen=True, slots=True)
class ThreadPeerInfo:
    """One caller-facing thread peer."""

    type: ThreadPeerType = "user"
    name: str = "user"
    thread: str | None = None


@dataclass(frozen=True, slots=True)
class FailureDetail:
    """One normalized run failure schema."""

    reason: str
    step_index: int | None = None
    step_kind: StepKind | None = None
    step_error: str | None = None


@dataclass(frozen=True, slots=True)
class ThreadRunInfo:
    """One compact run summary embedded in a thread summary."""

    id: str
    origin: str
    status: RunStatus
    created_at: str
    started_at: str
    finished_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    """One thread summary schema."""

    id: str
    title: str
    created_at: str
    updated_at: str
    origin: str
    channel: str
    status: str
    peer: ThreadPeerInfo
    parent: str | None
    run_count: int
    latest_run: ThreadRunInfo | None
    active_run: ThreadRunInfo | None


@dataclass(frozen=True, slots=True)
class RunInfo:
    """One caller-facing run summary and identity schema."""

    id: str
    parent: str | None
    origin: str
    thread_id: str
    root_run_id: str
    executable_kind: str
    executable_name: str | None
    call_kind: str
    metadata: dict[str, object]
    input_text: str
    summary: str
    status: RunStatus
    error: str | None
    superseded: dict[str, object] | None
    failure: FailureDetail | None
    created_at: str
    started_at: str
    finished_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class CommandInfo:
    """One accepted command sent to a run."""

    run_id: str
    index: int
    kind: CommandKind
    apply: CommandApply
    status: CommandStatus
    message: MessageData | None
    error: str | None
    created_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class RunCommandResult:
    """One accepted run command and the current run schema."""

    run: RunInfo
    command: CommandInfo


@dataclass(frozen=True, slots=True)
class ThreadResult:
    """One thread mutation result with an optional accepted follow-up run."""

    thread: ThreadInfo
    run: RunCommandResult | None = None


@dataclass(frozen=True, slots=True)
class ChatResult:
    """One completed chat exchange."""

    thread: ThreadInfo
    run: RunInfo
    message: MessageData
    assistant: MessageData


@dataclass(frozen=True, slots=True)
class StepData:
    """One caller-facing execution step."""

    parent: StepPath
    index: int
    kind: StepKind
    input: list[StepInputData]
    output: list[MessagePartData]
    context: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    status: StepStatus = "running"
    error: str | None = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str | None = None


@dataclass(frozen=True, slots=True)
class CommandData:
    """One caller-facing run command."""

    run: str
    index: int
    kind: CommandKind
    apply: CommandApply
    input: MessagePayload | None
    context: dict[str, Any] = field(default_factory=dict)
    status: CommandStatus = "pending"
    error: str | None = None
    created_at: str = ""
    finished_at: str | None = None


@dataclass(frozen=True, slots=True)
class StepDetail:
    """One step detail schema."""

    record: StepData
    message: MessageData | None
    virtual: bool = False


@dataclass(frozen=True, slots=True)
class InputDetail:
    """One run input detail schema."""

    record: CommandData
    message: MessageData | None


@dataclass(frozen=True, slots=True)
class RunOutput:
    """One run output schema."""

    status: RunStatus
    error: str | None
    failure: FailureDetail | None
    steps: list[StepDetail] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RunDetail(RunInfo):
    """One complete run detail schema."""

    input: MessageData | None
    inputs: list[InputDetail]
    output: RunOutput
    prompts: dict[str, str] = field(default_factory=dict)
    event_cursor: int | None = None


@dataclass(frozen=True, slots=True)
class ThreadDetail(ThreadInfo):
    """One complete thread detail schema."""

    runs: list[RunDetail] = field(default_factory=list)
    event_cursor: int | None = None
