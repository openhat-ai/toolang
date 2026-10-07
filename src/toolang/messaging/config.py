"""Messaging configuration; callers resolve files and environment defaults."""

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, cast

from valkey.asyncio.connection import parse_url

from .errors import MessagingError
from .schemas import component, conversation


@dataclass(frozen=True)
class MessagingConfig:
    url: str
    groups: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        return sha256(self.url.encode()).hexdigest()[:20]

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "MessagingConfig | None":
        raw = config.get("messaging")
        if raw is None:
            return None
        if not isinstance(raw, Mapping) or not isinstance(raw.get("url"), str):
            raise MessagingError("messaging.url must name an external Valkey server")
        try:
            parse_url(raw["url"])
        except (ValueError, TypeError) as exc:
            raise MessagingError("Invalid messaging.url") from exc
        groups = raw.get("groups", [])
        if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            raise MessagingError(
                "messaging.groups must be a list of gc_ conversation IDs"
            )
        for group in groups:
            if conversation(group).kind != "group":
                raise MessagingError(
                    "messaging.groups only declares custom gc_ groups; DMs and all are automatic"
                )
        return cls(raw["url"], tuple(dict.fromkeys(cast(list[str], groups))))


def human_name(config: Mapping[str, Any], *, default: str) -> str:
    raw = config.get("human", {})
    if not isinstance(raw, Mapping):
        raise MessagingError("human must be a table")
    name = raw.get("name", default)
    if not isinstance(name, str):
        raise MessagingError("human.name must be text")
    component(name)
    return name
