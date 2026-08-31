"""HTTP request and response schemas."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from toolang.catalog.types import DEFAULT_CHORE_SCHEDULE
from toolang.base.types.model import ModelRequest
from toolang.base.types.policy import RunLimits, RunPolicy
from toolang.execution.schemas import (
    ControlInfo,
    RunnableRequest,
    RunDetail,
    ThreadInfo,
)
from toolang.execution.types import StepPath
from toolang.lang.types import parse_public_runnable_ref


NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
StrictInt = Annotated[int, Field(strict=True)]
StrictText = Annotated[str, Field(strict=True)]


def _reject_keys(value: object, allowed: set[str], location: str) -> None:
    if not isinstance(value, Mapping):
        return
    unknown = set(value) - allowed
    if unknown:
        joined = ", ".join(sorted(str(item) for item in unknown))
        raise ValueError(f"unknown {location} fields: {joined}")


def _reject_materialized_run_unknowns(value: object, *, direct: bool) -> None:
    """Keep nested standard-dataclass request schemas closed at the HTTP edge."""

    if not isinstance(value, Mapping):
        return
    data = cast(Mapping[str, object], value)
    _reject_keys(
        data,
        {"thread_id", "request_id", "runnable", "model", "policy"},
        "run request",
    )
    runnable = data.get("runnable")
    _reject_keys(
        runnable,
        {"ref", "input", "args"} if direct else {"ref", "input"},
        "runnable request",
    )
    if isinstance(runnable, Mapping):
        runnable_ref = cast(Mapping[str, object], runnable).get("ref")
        if isinstance(runnable_ref, str):
            _name, kind = parse_public_runnable_ref(runnable_ref)
            if kind is None:
                raise ValueError("runnable request requires a kind-qualified ref")
    if not direct and isinstance(runnable, Mapping):
        runnable_data = cast(Mapping[str, object], runnable)
        raw_input = runnable_data.get("input")
        _reject_keys(raw_input, {"_", "named"}, "runnable input")
        if isinstance(raw_input, Mapping):
            named = cast(Mapping[str, object], raw_input).get("named")
            if isinstance(named, list | tuple):
                for item in named:
                    _reject_keys(item, {"name", "source"}, "named input")
    model = data.get("model")
    _reject_keys(model, {"ref", "parameters"}, "model request")
    if isinstance(model, Mapping):
        model_data = cast(Mapping[str, object], model)
        parameters = model_data.get("parameters")
        _reject_keys(parameters, {"reasoning"}, "model parameters")
        if isinstance(parameters, Mapping):
            parameters_data = cast(Mapping[str, object], parameters)
            _reject_keys(
                parameters_data.get("reasoning"),
                {"effort", "budget_tokens"},
                "reasoning parameters",
            )
    policy = data.get("policy")
    _reject_keys(policy, {"allow", "limits"}, "run policy")
    if isinstance(policy, Mapping):
        policy_data = cast(Mapping[str, object], policy)
        allow = policy_data.get("allow")
        if isinstance(allow, list | tuple):
            for item in allow:
                _reject_keys(
                    item,
                    {
                        "models",
                        "tools",
                        "psyches",
                        "skills",
                        "services",
                        "prompts",
                    },
                    "allow ceiling",
                )
        raw_limits = policy_data.get("limits")
        _reject_keys(
            raw_limits,
            {"agic_model_calls", "agic_tool_calls", "tokens", "cost", "time"},
            "run limits",
        )
        _reject_run_limit_types(raw_limits)


def _reject_run_limit_types(value: object) -> None:
    """Reject lossy coercion of integer run limits at the HTTP boundary."""

    if not isinstance(value, Mapping):
        return
    limits = cast(Mapping[str, object], value)
    for field in ("agic_model_calls", "agic_tool_calls", "tokens", "time"):
        raw = limits.get(field)
        if raw is not None and (isinstance(raw, bool) or not isinstance(raw, int)):
            raise ValueError(f"run limit {field} must be an integer or null")


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

    scope: Literal["home", "root"] = "home"
    content: str | None = None


class ConfiguredCapRequest(ApiRequest):
    """One configured cap ref mutation request."""

    scope: Literal["home", "root"] = "home"
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


class DirectRunnableRequest(ApiRequest):
    """One direct runnable ref grouped with its resolved input representation."""

    ref: str = Field(min_length=1)
    input: list[InputPart] = Field(default_factory=list)
    args: dict[str, object] | None = None


class RunCreateRequest(ApiRequest):
    """One non-interactive agic or flow execution request."""

    thread_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    runnable: DirectRunnableRequest
    model: ModelRequest | None
    policy: RunPolicy

    @model_validator(mode="before")
    @classmethod
    def reject_unknown_request_fields(cls, value: object) -> object:
        _reject_materialized_run_unknowns(value, direct=True)
        return value


class RunOverridePayload(ApiRequest):
    """One unresolved run policy override."""

    group: Literal["allow", "default", "limit"]
    field: StrictText
    value: list[StrictText] | StrictText | StrictInt | None


class AuthoredRunRequest(ApiRequest):
    """One materialized authored run request."""

    thread_id: StrictText
    request_id: StrictText
    runnable: RunnableRequest
    model: ModelRequest | None
    policy: RunPolicy

    @model_validator(mode="before")
    @classmethod
    def reject_unknown_request_fields(cls, value: object) -> object:
        _reject_materialized_run_unknowns(value, direct=False)
        return value


class AuthoredRerunRequest(ApiRequest):
    """One unresolved rerun request for server-owned resolution."""

    request_id: StrictText
    commands: list[RunOverridePayload] = Field(default_factory=list)
    model: ModelRequest | None = None


class AuthoredRetryRequest(ApiRequest):
    """One unresolved retry request for server-owned resolution."""

    request_id: StrictText
    commands: list[RunOverridePayload] = Field(default_factory=list)
    anchor: StepPath | None = None


class RuntimeSandboxPayload(ApiRequest):
    """Public identity of the sandbox hosting the current server process."""

    driver: StrictText
    selector: StrictText
    instance: StrictText | None = None
    description: StrictText | None = None


class RuntimeIdentityPayload(ApiRequest):
    """Source version and sandbox identity of the current server process."""

    version: StrictText
    sandbox: RuntimeSandboxPayload


class RunRerunRequest(ApiRequest):
    """Request a new run from one source invocation."""

    request_id: str | None = Field(default=None, min_length=1)
    model: ModelRequest | None = None
    limits: RunLimitsPayload | None = None


class RunRetryRequest(ApiRequest):
    """Request retry from one durable step boundary."""

    request_id: str | None = Field(default=None, min_length=1)
    anchor: StepPath | None = None
    limits: RunLimitsPayload | None = None


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
