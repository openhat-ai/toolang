"""Durable execution record types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

from toolang.base.types.message import Message, Part


RunStatus = Literal["running", "finished", "failed", "canceled"]
StepStatus = Literal["finished", "failed", "canceled"]
RunLoop = str
StepKind = Literal["model_call", "tool_call", "runtime"]
ThreadPeerType = Literal["user", "agent"]
InputAction = Literal["start", "steer", "stop"]
InputMode = Literal["immediate", "next_step", "next_call"]

UpdateKind = Literal[
    "created",
    "started",
    "stopped",
    "removed",
    "program_changed",
    "config_changed",
    "psyche_changed",
    "prompt_changed",
    "service_changed",
    "skill_changed",
    "task_changed",
    "chore_changed",
]
EventDomain = Literal["agent", "thread", "run"]


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Durable run truth."""

    run_id: str
    thread_id: str
    origin: str
    status: RunStatus
    error: str | None
    superseded: dict[str, Any] | None
    created_at: str
    started_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class ThreadPeer:
    """One local thread peer descriptor."""

    type: ThreadPeerType = "user"
    name: str = "user"
    thread: str | None = None

    @classmethod
    def from_data(cls, payload: Mapping[str, Any] | None) -> "ThreadPeer":
        if payload is None:
            return cls()
        peer_type = str(payload.get("type", "user")).strip() or "user"
        if peer_type not in {"user", "agent"}:
            raise ValueError(f"unsupported thread peer type: {peer_type}")
        name = str(payload.get("name", "user" if peer_type == "user" else "")).strip()
        if peer_type == "agent" and not name:
            raise ValueError("agent thread peer requires name")
        raw_thread = payload.get("thread")
        thread = str(raw_thread).strip() if raw_thread is not None else None
        return cls(
            type=cast(ThreadPeerType, peer_type),
            name=name or "user",
            thread=thread or None,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "thread": self.thread,
        }


@dataclass(frozen=True, slots=True)
class ThreadRecord:
    """Durable thread metadata."""

    thread_id: str
    origin: str
    peer: ThreadPeer
    parent: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class RunInputRef:
    """Reference one run input item or one input part."""

    index: int = 0
    part_index: int | None = None

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> RunInputRef:
        raw_index = payload.get("index", 0)
        part_index = payload.get("part")
        return cls(
            index=int(raw_index) if raw_index is not None else 0,
            part_index=int(part_index) if part_index is not None else None,
        )

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": "input", "index": self.index}
        if self.part_index is not None:
            data["part"] = self.part_index
        return data


@dataclass(frozen=True, slots=True)
class StepOutputRef:
    """Reference one prior step output or one output part."""

    step_index: int
    part_index: int | None = None

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> StepOutputRef:
        raw_index = payload.get("index", 0)
        part_index = payload.get("part")
        return cls(
            step_index=int(raw_index),
            part_index=int(part_index) if part_index is not None else None,
        )

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": "step",
            "index": self.step_index,
        }
        if self.part_index is not None:
            data["part"] = self.part_index
        return data


StepInputItem = RunInputRef | StepOutputRef | Message


@dataclass(frozen=True, slots=True)
class ModelCallStepPayload:
    """One model-call step payload."""

    model_ref: str
    input_tokens: int
    output_tokens: int
    provider: str = ""
    model: str = ""
    adapter: str = ""
    base_url: str | None = None
    instruct: str | None = None
    context: str | None = None
    reasoning_content: str | None = None

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> ModelCallStepPayload:
        instruct = payload.get("instruct", payload.get("instructions_hash"))
        return cls(
            model_ref=str(payload.get("model_ref", "")),
            input_tokens=int(payload.get("input_tokens", 0)),
            output_tokens=int(payload.get("output_tokens", 0)),
            provider=str(payload.get("provider", "")),
            model=str(payload.get("model", "")),
            adapter=str(payload.get("adapter", "")),
            base_url=(
                str(payload.get("base_url"))
                if payload.get("base_url") is not None
                else None
            ),
            instruct=(
                str(instruct)
                if instruct is not None
                else None
            ),
            context=(
                str(payload.get("context"))
                if payload.get("context") is not None
                else None
            ),
            reasoning_content=(
                str(payload.get("reasoning_content"))
                if payload.get("reasoning_content") is not None
                else None
            ),
        )

    def to_data(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ToolCallStepPayload:
    """One tool-call step payload."""

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> ToolCallStepPayload:
        del payload
        return cls()

    def to_data(self) -> dict[str, Any]:
        return {}


@dataclass(frozen=True, slots=True)
class RuntimeStepPayload:
    """One runtime step payload."""

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> RuntimeStepPayload:
        del payload
        return cls()

    def to_data(self) -> dict[str, Any]:
        return {}


StepPayload = ModelCallStepPayload | ToolCallStepPayload | RuntimeStepPayload


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One durable step record."""

    run_id: str
    step_index: int
    kind: StepKind
    status: StepStatus
    input: tuple[StepInputItem, ...]
    output: tuple[Part, ...]
    started_at: str
    finished_at: str
    payload: StepPayload
    error: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateRecord:
    """One durable agent-local update record."""

    update_id: int
    kind: UpdateKind
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One durable resource-scoped event record."""

    event_id: int
    domain: EventDomain
    domain_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class InputRecord:
    """One durable client-side run input."""

    run_id: str
    index: int
    action: InputAction
    mode: InputMode | None
    request_id: str | None
    message: Message | None
    created_at: str


def step_input_item_from_data(payload: Mapping[str, Any]) -> StepInputItem:
    """Return one step input item from one serialized payload."""

    kind = str(payload.get("kind", "")).strip()
    if kind == "input":
        return RunInputRef.from_data(payload)
    if kind == "step":
        return StepOutputRef.from_data(payload)
    if kind == "message":
        return Message.from_data(payload.get("message", {}))
    raise ValueError(f"unknown step input item kind: {kind or '<empty>'}")


def step_input_items_from_data(payloads: list[Mapping[str, Any]]) -> tuple[StepInputItem, ...]:
    """Return step input items from one serialized sequence."""

    return tuple(step_input_item_from_data(item) for item in payloads)


def step_input_item_to_data(item: StepInputItem) -> dict[str, Any]:
    """Return one serialized step input item."""

    if isinstance(item, RunInputRef):
        return item.to_data()
    if isinstance(item, StepOutputRef):
        return item.to_data()
    return {"kind": "message", "message": item.to_data()}


def step_input_items_to_data(items: tuple[StepInputItem, ...]) -> list[dict[str, Any]]:
    """Return serialized step input items."""

    return [step_input_item_to_data(item) for item in items]


def step_payload_from_data(kind: StepKind, payload: Mapping[str, Any]) -> StepPayload:
    """Return one step payload for one step kind."""

    if kind == "model_call":
        return ModelCallStepPayload.from_data(payload)
    if kind == "tool_call":
        return ToolCallStepPayload.from_data(payload)
    return RuntimeStepPayload.from_data(payload)


def step_payload_to_data(payload: StepPayload) -> dict[str, Any]:
    """Return one serialized step payload."""

    return payload.to_data()


def step_input_messages(items: tuple[StepInputItem, ...]) -> tuple[Message, ...]:
    """Return inline input messages from one step input tuple."""

    return tuple(item for item in items if isinstance(item, Message))


def cast_message_role(role: str) -> Literal["user", "assistant", "tool"]:
    """Return one normalized message role literal."""

    if role not in {"user", "assistant", "tool"}:
        raise ValueError(f"unsupported message role: {role}")
    return cast(Literal["user", "assistant", "tool"], role)
