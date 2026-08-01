"""HTTP request schemas."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from toolang.catalog.types import DEFAULT_CHORE_SCHEDULE
from toolang.execution.schemas import (
    RunControlInfo,
    RunDetail,
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
        if sum(value is not None for value in (self.image_url, self.file_id)) != 1:
            raise ValueError(
                "image part requires exactly one of image_url or file_id"
            )
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
    parts: list[InputPart] = Field(min_length=1)


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


class RunCreateRequest(ApiRequest):
    """One non-interactive agic or flow execution request."""

    thread: str = Field(min_length=1)
    request_id: str | None = Field(default=None, min_length=1)
    runnable: str = Field(min_length=1)
    input: list[InputPart] = Field(default_factory=list)
    model: str | None = None
    args: dict[str, object] | None = None


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
