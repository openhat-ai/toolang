"""Concrete messaging configuration supplied by setup; no loading or defaults."""

from dataclasses import dataclass
from hashlib import sha256

from valkey.asyncio.connection import parse_url

from .errors import MessagingError
from .schemas import conversation


@dataclass(frozen=True)
class MessagingConfig:
    url: str
    groups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            parse_url(self.url)
        except (ValueError, TypeError) as exc:
            raise MessagingError("Invalid messaging.url") from exc
        for group in self.groups:
            if conversation(group).kind != "group":
                raise MessagingError(
                    "messaging.groups only declares custom gc_ groups; DMs and all are automatic"
                )

    @property
    def identity(self) -> str:
        return sha256(self.url.encode()).hexdigest()[:20]
