"""HTTP request and response schemas."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from toolang.catalog.types import DEFAULT_CHORE_SCHEDULE
from toolang.base.types.policy import RunLimits
from toolang.execution.schemas import (
    ControlInfo,
    RunDetail,
    ThreadInfo,
)
from toolang.execution.types import StepPath


NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
StrictInt = Annotated[int, Field(strict=True)]
StrictText = Annotated[str, Field(strict=True)]


class ApiRequest(BaseModel):
    """Base class for strict public API request bodies."""

    model_config = ConfigDict(extra="forbid")


class TextInputPart(ApiRequest):
    """One text input part."""

    type: Literal["text"]
    text: str


class ImageInputPart(ApiRequest):
    """One image input part."""

    type: Literal["image"]
    image_url: str | None = None
    file_id: str | None = None
    detail: Literal["low", "high", "auto", "original"] = "auto"
    filename: str | None = None
    media_type: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if sum(value is not None for value in (self.image_url, self.file_id)) != 1:
            raise ValueError("image part requires exactly one of image_url or file_id")
        return self


class AudioInputPart(ApiRequest):
    """One audio input part."""

    type: Literal["audio"]
    data: str | None = None
    data_url: str | None = None
    format: Literal["mp3", "wav"] | None = None
    filename: str | None = None
    media_type: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if sum(value is not None for value in (self.data, self.data_url)) != 1:
            raise ValueError("audio part requires exactly one of data or data_url")
        return self


class DocumentInputPart(ApiRequest):
    """One document input part."""

    type: Literal["document"]
    data: str | None = None
    url: str | None = None
    file_id: str | None = None
    filename: str | None = None
    media_type: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if sum(value is not None for value in (self.data, self.url, self.file_id)) != 1:
            raise ValueError(
                "document part requires exactly one of data, url, or file_id"
            )
        return self


InputPart = Annotated[
    TextInputPart | ImageInputPart | AudioInputPart | DocumentInputPart,
    Field(discriminator="type"),
]


class InputMessagePayload(ApiRequest):
    """One user-authored input message."""

    role: Literal["user"] = "user"
    parts: list[InputPart]


class ThreadPeerPayload(ApiRequest):
    """One optional thread peer descriptor."""

    type: str = Field(default="user", min_length=1)
    name: str = Field(default="user", min_length=1)
    thread: str | None = None


class ThreadCreateRequest(ApiRequest):
    """Request one empty thread."""

    client: Literal["web", "term", "tui", "chat", "script"] = "term"
    peer: ThreadPeerPayload | None = None


class ThreadRewindRequest(ApiRequest):
    """Request a thread rewind from one anchor run."""

    run_id: str | None = Field(default=None, min_length=1)
    request_id: str | None = None


class ThreadForkRequest(ThreadRewindRequest):
    """Request a thread fork from one anchor run."""


class PutCapRequest(ApiRequest):
    """One authored cap write request."""

    visibility: Literal["private", "shared"] = "private"
    content: str | None = None


class WiredCapRequest(ApiRequest):
    """One wired cap ref mutation request."""

    visibility: Literal["private", "shared"] = "private"
    ref: str


class TaskCreateRequest(ApiRequest):
    """Request one authored task creation."""

    title: str | None = None
    body: str = ""


class TaskPatchRequest(ApiRequest):
    """Request one authored task update."""

    title: str | None = None
    body: str | None = None


class ChoreCreateRequest(ApiRequest):
    """Request one authored chore creation."""

    title: str | None = None
    body: str = ""
    schedule: str = DEFAULT_CHORE_SCHEDULE


class ChorePatchRequest(ApiRequest):
    """Request one authored chore update."""

    title: str | None = None
    body: str | None = None
    schedule: str | None = None


class RunLimitsPayload(ApiRequest):
    """One partial run-limit override."""

    agic_model_calls: NonNegativeInt | None = None
    agic_tool_calls: NonNegativeInt | None = None
    tokens: NonNegativeInt | None = None
    cost: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    time: NonNegativeInt | None = None

    def to_limits(self, base: RunLimits) -> RunLimits:
        """Overlay explicitly supplied fields on one effective default."""

        return replace(base, **self.model_dump(exclude_unset=True))


class RunCreateRequest(ApiRequest):
    """One non-interactive agic or flow execution request."""

    thread: str = Field(min_length=1)
    request_id: str | None = Field(default=None, min_length=1)
    runnable: str = Field(min_length=1)
    input: list[InputPart] = Field(default_factory=list)
    model: str | None = None
    args: dict[str, object] | None = None
    limits: RunLimitsPayload | None = None


class RunOverridePayload(ApiRequest):
    """One unresolved run policy override."""

    group: Literal["allow", "default", "limit"]
    field: StrictText
    value: list[StrictText] | StrictText | StrictInt | None


class NamedInputSourcePayload(ApiRequest):
    """One named authored input source."""

    name: StrictText
    source: StrictText


class RunnableInputRawPayload(ApiRequest):
    """Authored primary and named input awaiting server resolution."""

    primary: StrictText | None = None
    named: list[NamedInputSourcePayload] = Field(default_factory=list)


class AuthoredRunRequest(ApiRequest):
    """One unresolved run request for server-owned resolution."""

    thread: StrictText
    request_id: StrictText
    commands: list[RunOverridePayload] = Field(default_factory=list)
    input: RunnableInputRawPayload
    session_commands: list[RunOverridePayload] = Field(default_factory=list)
    runnable_fallbacks: list[StrictText] = Field(min_length=1)


class AuthoredRunValidationRequest(ApiRequest):
    """Complete session policy awaiting server-owned validation."""

    session_commands: list[RunOverridePayload] = Field(default_factory=list)
    runnable_fallbacks: list[StrictText] = Field(min_length=1)


class RuntimeSandboxPayload(ApiRequest):
    """Public identity of the sandbox hosting the current server process."""

    driver: StrictText
    selector: StrictText
    instance: StrictText | None = None


class RuntimeIdentityPayload(ApiRequest):
    """Version and sandbox identity of the current server process."""

    version: StrictText
    sandbox: RuntimeSandboxPayload


class RunRerunRequest(ApiRequest):
    """Request a new run from one source invocation."""

    request_id: str | None = Field(default=None, min_length=1)
    model: str | None = None
    limits: RunLimitsPayload | None = None


class RunRetryRequest(RunRerunRequest):
    """Request retry from one durable step boundary."""

    anchor: StepPath | None = None


class RunCancelRequest(ApiRequest):
    """Request run cancellation."""

    reason: str | None = None
    mode: Literal["immediate", "next_step", "next_call"] = "immediate"
    request_id: str | None = None


class RunSteerRequest(ApiRequest):
    """Request one steering message for a running run."""

    request_id: str | None = None
    mode: Literal["immediate", "next_step", "next_call"] = "next_step"
    message: InputMessagePayload


class ThreadResult(BaseModel):
    """One HTTP thread mutation response."""

    thread: ThreadInfo


class RunCommandResult(BaseModel):
    """One accepted HTTP run control."""

    run: RunDetail
    command: ControlInfo
