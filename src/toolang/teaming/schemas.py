"""Vendor-independent teaming identities and message records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Annotated, Any, Literal, cast
import unicodedata
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .errors import MessagingError

TargetKind = Literal["agent", "human", "group"]
_STREAM_ID = re.compile(r"[0-9]+-[0-9]+\Z")


def identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or unicodedata.category(value[0])[0] not in "LN"
        or any(
            unicodedata.category(c)[0] not in "LMN" and c not in "-_." for c in value
        )
    ):
        raise MessagingError(
            "ID must start with a letter/number and contain only letters, marks, numbers, -_."
        )
    return value


@dataclass(frozen=True)
class Target:
    kind: TargetKind
    name: str

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.name}"


def target(value: str, *, kind: TargetKind | None = None) -> Target:
    if not isinstance(value, str):
        raise MessagingError("Target must be text")
    prefix, sep, name = value.partition(":")
    if not sep or prefix not in {"agent", "human", "group"}:
        raise MessagingError(f"Invalid target: {value}; use agent:, human:, or group:")
    if kind is not None and prefix != kind:
        raise MessagingError(f"Expected a {kind} target: {value}")
    return Target(cast(TargetKind, prefix), identifier(name))


def participant(value: str) -> Target:
    result = target(value)
    if result.kind == "group":
        raise MessagingError("A group is not a participant")
    return result


def direct_pair(a: str, b: str) -> str:
    participant(a)
    participant(b)
    if a == b:
        raise MessagingError("Direct participants must be distinct")
    return json.dumps(
        sorted((a, b), key=str.encode), ensure_ascii=False, separators=(",", ":")
    )


@dataclass(frozen=True)
class Conversation:
    id: str
    kind: Literal["group", "direct"]
    members: tuple[str, ...]
    display_name: str | None = None
    system: bool = False

    @property
    def label(self) -> str:
        return self.display_name or (
            " ↔ ".join(self.members) if self.kind == "direct" else target(self.id).name
        )

    def allows_sender(self, sender: str) -> bool:
        return sender in self.members


def stream_id(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not _STREAM_ID.fullmatch(value):
        raise MessagingError("Invalid Stream ID")
    a, b = value.split("-")
    return int(a), int(b)


@dataclass(frozen=True)
class Message:
    id: str
    sender: str
    body: str
    in_reply_to: str | None = None
    origin: dict[str, str | None] | None = None

    def __post_init__(self) -> None:
        try:
            UUID(self.id)
            if self.in_reply_to is not None:
                UUID(self.in_reply_to)
        except (ValueError, AttributeError, TypeError) as exc:
            raise MessagingError("Message and reply IDs must be UUIDs") from exc
        who = participant(self.sender)
        if (
            not isinstance(self.body, str)
            or not self.body.strip()
            or len(self.body.encode()) > 262144
        ):
            raise MessagingError("Message body must be nonblank text up to 256 KiB")
        if self.origin is not None and (
            who.kind != "agent"
            or not isinstance(self.origin, dict)
            or set(self.origin) != {"thread", "run"}
            or any(
                v is not None and not isinstance(v, str) for v in self.origin.values()
            )
        ):
            raise MessagingError("Invalid message origin")

    @classmethod
    def create(
        cls,
        sender: str,
        body: str,
        in_reply_to: str | None = None,
        origin: dict[str, str | None] | None = None,
    ) -> Message:
        return cls(str(uuid4()), sender, body, in_reply_to, origin)

    @classmethod
    def decode(cls, data: str) -> Message:
        try:
            return cls(**json.loads(data))
        except (TypeError, ValueError) as exc:
            raise MessagingError("Malformed message JSON") from exc

    def data(self) -> dict[str, Any]:
        return asdict(self)

    def encode(self) -> str:
        return json.dumps(self.data(), ensure_ascii=False)


class HubRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResolveRequest(HubRequest):
    target: str
    kind: Literal["dm", "group"] | None = None


class CreateGroupRequest(HubRequest):
    name: str


class SendRequest(HubRequest):
    id: str
    target: str
    body: str
    in_reply_to: str | None = None


class AgentSendRequest(SendRequest):
    thread: str | None = None
    run: str | None = None


class AgentRegistration(HubRequest):
    endpoint: str = Field(default="", max_length=2048)
    managed: bool = True


Generation = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class PublicationRequest(HubRequest):
    v: Literal[1]
    epoch: Generation
    generation: Generation


class IncompleteRequest(PublicationRequest):
    kind: Literal["incomplete"]
    id: str
    recovery: Generation
    reason: str = Field(min_length=1, max_length=256)


class RecoveryRequest(PublicationRequest):
    kind: Literal["recover"]
    id: str
    recovery: Generation
    source: str
    source_epoch: Generation
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_generation: Generation


class EventRequest(PublicationRequest):
    kind: Literal["event"]
    id: str
    source: str
    source_epoch: Generation
    prior: str
    data: str
    updates: dict[str, str]
    removed: list[str]
    structural: bool
    evicted: bool
    count: int = Field(ge=0)
    bytes: int = Field(ge=0)


CommitRequest = Annotated[
    IncompleteRequest | RecoveryRequest | EventRequest, Field(discriminator="kind")
]


class StagingRequest(HubRequest):
    v: Literal[1]
    epoch: Generation
    recovery: Generation
    source: str
    entities: dict[str, str]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class HistoryEntry(BaseModel):
    stream_id: str
    data: str | None = None


@dataclass(frozen=True)
class HubConnection:
    endpoint: str
    human: str
    identity: str
