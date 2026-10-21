"""Shared capability-state vocabulary and scalar types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Mapping, cast


RUNTIME_TALK_MODULE = "_"
RUNTIME_TALK_SOURCE = "runtime/talk.too"

CapScope = Literal["root", "home", "here"]
EntryKind = Literal["psyche", "skill", "service", "prompt"]
EntryShape = Literal["file", "dir"]
SourceOrigin = Literal["local", "remote"]
CapForm = Literal["authored", "inline", "configured", "referenced"]
ProgramKind = Literal["agent", "runnable"]
RunnableKind = Literal["agic", "flow"]


@dataclass(frozen=True, slots=True)
class StateFile:
    """One observed source and its independent acceptance receipt."""

    scope: Literal["root", "home"]
    key: str
    digest: str | None
    accepted: bool = True
    code: str | None = None
    message: str | None = None
    line: int | None = None

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "scope": self.scope,
            "key": self.key,
            "observed_digest": self.digest,
            "accepted_digest": self.digest if self.accepted else None,
        }
        if self.code is not None:
            data.update(code=self.code, message=self.message)
            if self.line is not None:
                data["line"] = self.line
        return data

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> StateFile:
        required = {"scope", "key", "observed_digest", "accepted_digest"}
        if not required <= data.keys() or data.keys() - required - {
            "code",
            "message",
            "line",
        }:
            raise ValueError("source receipt fields do not match the schema")
        scope = data["scope"]
        key = data["key"]
        observed = data["observed_digest"]
        accepted = data["accepted_digest"]
        if (
            not isinstance(scope, str)
            or scope not in {"root", "home"}
            or not isinstance(key, str)
        ):
            raise ValueError("invalid source receipt identity")
        if not key or key.startswith("/") or ".." in key.split("/") or "\\" in key:
            raise ValueError("source receipt key must be portable")
        for digest in (observed, accepted):
            if digest is not None and (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("source receipt digest must be SHA-256")
        if accepted is not None and data.keys() & {"code", "message", "line"}:
            raise ValueError("accepted source cannot contain a rejection diagnostic")
        if accepted is not None and accepted != observed:
            raise ValueError("accepted source must match observed bytes")
        code = data.get("code")
        message = data.get("message")
        line = data.get("line")
        if accepted is None and (
            not isinstance(code, str) or not isinstance(message, str)
        ):
            raise ValueError("excluded source requires a diagnostic")
        if code is not None and (
            not isinstance(code, str) or not isinstance(message, str)
        ):
            raise ValueError("invalid source diagnostic")
        if line is not None and (
            not isinstance(line, int) or isinstance(line, bool) or line < 1
        ):
            raise ValueError("invalid source diagnostic line")
        return cls(
            cast(Literal["root", "home"], scope),
            key,
            cast(str | None, observed),
            accepted is not None,
            code,
            cast(str | None, message),
            line,
        )


StateSyncError = Literal["state_rejected", "io_error"]


@dataclass(frozen=True, slots=True)
class StateSyncResult:
    """One publication receipt or a rejected source with its last-valid State."""

    revision: str | None
    files: tuple[StateFile, ...]
    error: StateSyncError | None = None
    message: str | None = None

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "revision": self.revision,
            "files": [item.to_data() for item in self.files]
            if self.error is None
            else [],
        }
        if self.error is not None:
            data.update(
                error=self.error,
                message=self.message,
            )
        return data


StateSync = Callable[[], Awaitable[StateSyncResult]]
