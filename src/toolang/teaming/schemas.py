"""Vendor-independent teaming identities and message records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import re
from typing import Annotated, Any, Literal, cast
import unicodedata
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    AfterValidator,
    field_validator,
    model_validator,
    with_config,
)
from typing_extensions import TypedDict, NotRequired

from .errors import MessagingError
from .types import MAX_SAFE_INTEGER

TargetKind = Literal["agent", "human"]
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
    if not sep or prefix not in {"agent", "human"}:
        raise MessagingError(f"Invalid target: {value}; use agent: or human:")
    if kind is not None and prefix != kind:
        raise MessagingError(f"Expected a {kind} target: {value}")
    return Target(cast(TargetKind, prefix), identifier(name))


def participant(value: str) -> Target:
    return target(value)


def direct_pair(a: str, b: str) -> str:
    participant(a)
    participant(b)
    if a == b:
        raise MessagingError("Direct participants must be distinct")
    return json.dumps(
        sorted((a, b), key=str.encode), ensure_ascii=False, separators=(",", ":")
    )


def conversation_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:dm|gc)_[0123456789abcdefghjkmnpqrstvwxyz]{8}", value
    ):
        raise MessagingError("Invalid conversation ID")
    return value


def conversation_name(value: str | None) -> str | None:
    if value is not None and (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not value.strip()
        or value != value.strip()
        or any(unicodedata.category(c).startswith("C") for c in value)
    ):
        raise MessagingError(
            "Conversation name must be 1–128 characters without controls or surrounding whitespace"
        )
    return value


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def utc_timestamp(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value):
        raise ValueError("Expected a UTC RFC 3339 timestamp")
    datetime.fromisoformat(value)
    return value


class ConversationRecord(ProtocolModel):
    id: str
    kind: Literal["dm", "gc"]
    name: str | None
    created_by: str | None
    created_at: str
    updated_at: str
    revision: int = Field(ge=1, le=MAX_SAFE_INTEGER)

    _id = field_validator("id")(conversation_id)
    _name = field_validator("name")(conversation_name)
    _timestamps = field_validator("created_at", "updated_at")(utc_timestamp)

    @field_validator("created_by")
    @classmethod
    def creator(cls, value: str | None) -> str | None:
        if value is not None:
            participant(value)
        return value

    @model_validator(mode="after")
    def identity(self):
        if not self.id.startswith(self.kind + "_"):
            raise ValueError("Conversation ID and kind disagree")
        return self


class LeaseRecord(ProtocolModel):
    token: str = Field(min_length=1)
    endpoint: str


class TeamRecord(ProtocolModel):
    display_name: str
    owner: str | None
    created_at: str
    lease: LeaseRecord | None

    _timestamp = field_validator("created_at")(utc_timestamp)

    @field_validator("owner")
    @classmethod
    def human_owner(cls, value: str | None) -> str | None:
        if value is not None:
            target(value, kind="human")
        return value


class RosterRecord(ProtocolModel):
    """Root ownership and discovery state for an agent in the team directory."""

    root: str = Field(min_length=1)
    managed: bool
    missing: int = Field(ge=0, le=MAX_SAFE_INTEGER)

    @model_validator(mode="after")
    def discovery(self):
        if not self.managed and self.missing:
            raise ValueError("Transient registrations have no discovery misses")
        return self


class TeamEvent(ProtocolModel):
    v: Literal[1]
    epoch: str = Field(pattern=r"^[0-9a-f]{32}$")
    type: Literal[
        "stream.initialized",
        "team.member_added",
        "team.member_removed",
        "conversation.member_added",
        "conversation.member_removed",
        "presence.online",
        "presence.updated",
        "presence.offline",
    ]
    conversation: str | None
    actor: str | None
    payload: dict[str, Any]

    @field_validator("v", mode="before")
    @classmethod
    def version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Event version must be an integer")
        return value

    @model_validator(mode="after")
    def envelope(self):
        if self.actor is not None:
            participant(self.actor)
        if self.type.startswith("conversation."):
            if self.conversation is None:
                raise ValueError("Missing event conversation")
            conversation_id(self.conversation)
        elif self.conversation is not None:
            raise ValueError("Unexpected event conversation")
        fields = set()
        if self.type != "stream.initialized":
            fields.add("member")
            participant(self.payload["member"])
        elif self.actor is not None:
            raise ValueError("Unexpected initialization actor")
        if self.type.startswith("presence."):
            target(self.payload["member"], kind="agent")
            fields.update(("effective_at_ms", "observed_at_ms"))
            for name in ("effective_at_ms", "observed_at_ms"):
                value = self.payload.get(name)
                if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
                    raise ValueError("Invalid presence timestamp")
        if self.type == "presence.updated":
            fields.add("changed")
            if self.payload.get("changed") != ["endpoint"]:
                raise ValueError("Invalid presence changes")
        if self.type == "presence.offline":
            fields.add("reason")
            if self.payload.get("reason") not in {"expired", "released"}:
                raise ValueError("Invalid offline reason")
        if set(self.payload) != fields:
            raise ValueError("Invalid event payload fields")
        return self


def member_id(value: str) -> str:
    participant(value)
    return value


MemberID = Annotated[str, AfterValidator(member_id)]
ConvoID = Annotated[str, AfterValidator(conversation_id)]
Count = Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER)]


@with_config(ConfigDict(extra="forbid", strict=True))
class TeamMember(TypedDict):
    member: MemberID
    display_name: str
    owner: MemberID | None
    created_at: Annotated[str, AfterValidator(utc_timestamp)]
    deadline: Annotated[float, Field(ge=0, le=MAX_SAFE_INTEGER)] | None
    online: NotRequired[bool | None]


@with_config(ConfigDict(extra="forbid", strict=True))
class MessagePreview(TypedDict):
    sender: MemberID
    body: str


@with_config(ConfigDict(extra="forbid", strict=True))
class ConversationSummary(TypedDict):
    conversation: ConvoID
    name: Annotated[str | None, AfterValidator(conversation_name)]
    kind: Literal["dm", "gc"]
    revision: Annotated[int, Field(ge=1, le=MAX_SAFE_INTEGER)]
    participants: list[MemberID]
    latest: str | None
    preview: NotRequired[MessagePreview | None]


@with_config(ConfigDict(extra="forbid", strict=True))
class Targets(TypedDict):
    participants: list[TeamMember]
    conversations: list[ConversationSummary]


@with_config(ConfigDict(extra="forbid", strict=True))
class GlobalStatistics(TypedDict):
    conversations_total: Count
    dm_count: Count
    gc_count: Count
    messages_total: Count


@with_config(ConfigDict(extra="forbid", strict=True))
class ConversationStatistics(TypedDict):
    participants_count: Count
    messages_total: Count
    messages_retained: Count
    last_message_stream_id: str | None


@dataclass(frozen=True)
class Conversation:
    id: str
    kind: Literal["gc", "dm"]
    participants: tuple[str, ...]
    name: str | None = None
    created_by: str | None = field(kw_only=True)
    created_at: str = field(kw_only=True)
    updated_at: str = field(kw_only=True)
    revision: int = field(kw_only=True)

    def __post_init__(self) -> None:
        metadata = asdict(self)
        metadata.pop("participants")
        ConversationRecord.model_validate(metadata)
        _participants(self.participants, direct=self.kind == "dm")

    @property
    def label(self) -> str:
        return self.name or (
            " ↔ ".join(self.participants) if self.kind == "dm" else self.id
        )

    def allows_sender(self, sender: str) -> bool:
        return sender in self.participants


def _participants(members: tuple[str, ...], *, direct: bool) -> None:
    if not isinstance(members, tuple) or len(set(members)) != len(members):
        raise MessagingError("Participants must be a tuple of distinct identities")
    for member in members:
        participant(member)
    if direct and len(members) != 2:
        raise MessagingError("A DM requires exactly two participants")


@dataclass(frozen=True)
class PendingDM:
    """Local selection before the first message; never a persisted record."""

    id: str
    participants: tuple[str, ...]
    kind: Literal["dm"] = field(default="dm", init=False)
    name: None = field(default=None, init=False)

    def __post_init__(self) -> None:
        conversation_id(self.id)
        if not self.id.startswith("dm_"):
            raise MessagingError("Pending conversations must be DMs")
        _participants(self.participants, direct=True)

    @property
    def label(self) -> str:
        return " ↔ ".join(self.participants)

    def allows_sender(self, sender: str) -> bool:
        return sender in self.participants


ConversationView = Conversation | PendingDM


@dataclass(frozen=True)
class Resolution:
    conversation: str
    participants: tuple[str, ...]
    exists: bool

    def __post_init__(self) -> None:
        conversation_id(self.conversation)
        _participants(self.participants, direct=self.conversation.startswith("dm_"))
        if type(self.exists) is not bool:
            raise MessagingError("Conversation existence must be boolean")


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
    kind: Literal["name"] | None = None
    create: bool = False


class CreateConversationRequest(HubRequest):
    name: str | None = None
    participants: list[str] | None = None


class RenameConversationRequest(HubRequest):
    name: str | None
    revision: int = Field(ge=1)


class SendRequest(HubRequest):
    id: str
    target: str
    participants: list[str] | None = None
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
