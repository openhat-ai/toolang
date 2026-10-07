"""Canonical conversation names and JSON wire messages; no runtime dependencies."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Any, Literal
from urllib.parse import quote, unquote
from uuid import uuid4

from .errors import MessagingError

_COMPONENT = re.compile(r"(?:[A-Za-z0-9-]|%[0-9A-F]{2})+\Z")
_STREAM_ID = re.compile(r"[0-9]+-[0-9]+\Z")


def component(name: str) -> str:
    if not name or not name.strip() or any(ord(c) < 32 for c in name):
        raise MessagingError(
            "Participant and group names must be nonempty printable text"
        )
    return (
        quote(name, safe="-")
        .replace("_", "%5F")
        .replace(".", "%2E")
        .replace("~", "%7E")
    )


def decode_component(value: str) -> str:
    try:
        decoded = unquote(value, errors="strict")
    except UnicodeError as exc:
        raise MessagingError("Invalid UTF-8 name") from exc
    if not _COMPONENT.fullmatch(value) or component(decoded) != value:
        raise MessagingError("Noncanonical name encoding")
    return decoded


@dataclass(frozen=True)
class Conversation:
    id: str
    kind: Literal["public", "group", "owner", "dm"]
    names: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return "all" if self.kind == "public" else " ↔ ".join(self.names)


def conversation(group: str) -> Conversation:
    if group == "all":
        return Conversation(group, "public")
    prefix, _, rest = group.partition("_")
    parts = rest.split("_")
    if prefix == "gc" and len(parts) == 1:
        return Conversation(group, "group", (decode_component(rest),))
    if prefix == "dm" and len(parts) in (1, 2):
        names = tuple(decode_component(p) for p in parts)
        if len(names) == 2 and (
            names[0] == names[1] or names != tuple(sorted(names, key=str.encode))
        ):
            raise MessagingError("Agent DM participants must be distinct and sorted")
        return Conversation(group, "owner" if len(names) == 1 else "dm", names)
    raise MessagingError(f"Invalid conversation ID: {group}")


def owner_dm(agent: str) -> str:
    return f"dm_{component(agent)}"


def agent_dm(a: str, b: str) -> str:
    result = "dm_" + "_".join(component(n) for n in sorted((a, b), key=str.encode))
    conversation(result)
    return result


def stream_id(value: str) -> tuple[int, int]:
    if not _STREAM_ID.fullmatch(value):
        raise MessagingError(f"Invalid Stream ID: {value}")
    a, b = value.split("-")
    return int(a), int(b)


@dataclass(frozen=True)
class Message:
    id: str
    sender: str
    body: str
    in_reply_to: str | None = None
    origin: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(v, str) and v.strip() for v in (self.id, self.sender, self.body)
        ):
            raise MessagingError("Message id, sender, and body must be nonempty text")
        if self.in_reply_to is not None and not isinstance(self.in_reply_to, str):
            raise MessagingError("in_reply_to must be a message ID or null")
        if self.origin is not None and not isinstance(self.origin, dict):
            raise MessagingError("origin must be an object or null")
        if len(self.body.encode()) > 262144:
            raise MessagingError("Message body exceeds 256 KiB")

    @classmethod
    def create(
        cls,
        sender: str,
        body: str,
        *,
        in_reply_to: str | None = None,
        origin: dict[str, Any] | None = None,
    ) -> Message:
        return cls(str(uuid4()), sender, body, in_reply_to, origin)

    @classmethod
    def decode(cls, data: str) -> Message:
        try:
            value = json.loads(data)
            return cls(**value)
        except (TypeError, ValueError) as exc:
            raise MessagingError("Malformed message JSON") from exc

    def data(self) -> dict[str, Any]:
        return asdict(self)

    def encode(self) -> str:
        return json.dumps(self.data(), ensure_ascii=False)
