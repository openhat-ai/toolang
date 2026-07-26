"""HTTP request schemas."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from toolang.base.types.message import Message
from toolang.catalog.types import DEFAULT_CHORE_SCHEDULE
from toolang.execution.schemas import (
    RunControlInfo,
    RunDetail,
    RunInfo,
    ThreadInfo,
)


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
        if self.image_url is None and self.file_id is None:
            raise ValueError("image part requires image_url or file_id")
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
        if self.data is None and self.data_url is None:
            raise ValueError("audio part requires data or data_url")
        return self


class FileInputPart(ApiRequest):
    """One file input part."""

    type: Literal["file"]
    file_data: str | None = None
    file_url: str | None = None
    file_id: str | None = None
    data_url: str | None = None
    filename: str | None = None
    media_type: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if not any((self.file_data, self.file_url, self.file_id, self.data_url)):
            raise ValueError(
                "file part requires file_data, file_url, file_id, or data_url"
            )
        return self


InputPart = Annotated[
    TextInputPart | ImageInputPart | AudioInputPart | FileInputPart,
    Field(discriminator="type"),
]


class InputMessagePayload(ApiRequest):
    """One user-authored input message."""

    role: Literal["user"] = "user"
    parts: list[InputPart] = Field(min_length=1)
    meta: dict[str, object] = Field(default_factory=dict)


class ThreadPeerPayload(ApiRequest):
    """One optional chat thread peer descriptor."""

    type: str = Field(default="user", min_length=1)
    name: str = Field(default="user", min_length=1)
    thread: str | None = None


class ThreadCreateRequest(ApiRequest):
    """Request one empty chat thread."""

    client: Literal["web", "term", "tui", "chat"] = "term"
    peer: ThreadPeerPayload | None = None


class ThreadRewindRequest(ApiRequest):
    """Request a thread rewind from one anchor run."""

    run_id: str | None = Field(default=None, min_length=1)
    request_id: str | None = None


class ThreadForkRequest(ThreadRewindRequest):
    """Request a thread fork from one anchor run."""


class ChatRequest(ApiRequest):
    """One formal chat submission."""

    thread: str | None = Field(default=None, min_length=1)
    client: Literal["web", "term", "tui", "chat"] = "web"
    peer: ThreadPeerPayload | None = None
    request_id: str | None = Field(default=None, min_length=1)
    message: InputMessagePayload
    model: str | None = None
    agic: str | None = None
    flow: str | None = None


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


class RunCreateRequest(ApiRequest):
    """One non-interactive agic or flow execution request."""

    executable_kind: Literal["agic", "flow"] = "agic"
    executable_name: str | None = None
    input: str = ""
    model: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


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
    command: RunControlInfo


class ChatResult(BaseModel):
    """One completed HTTP chat response."""

    thread: ThreadInfo
    run: RunInfo
    message: Message
    assistant: Message
