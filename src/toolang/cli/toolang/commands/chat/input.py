"""Terminal-chat input normalization and interaction parsing."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TypeAlias, TypeGuard

from toolang.execution.policy import parse_policy_prefix
from toolang.execution.types import RunOverride
from toolang.lang.input import RunnableInputRaw, parse_input


@dataclass(frozen=True, slots=True)
class QuickCommand:
    """One immediate terminal-chat command."""

    name: str
    tail: str | None = None


@dataclass(frozen=True, slots=True)
class RunOverrideHelp:
    """The special `:?` terminal-chat help interaction."""


ChatInput: TypeAlias = (
    QuickCommand | RunOverrideHelp | tuple[RunOverride, RunnableInputRaw]
)

_LEADING_BLANK_LINES_RE = re.compile(r"\A(?:[ \t]*(?:\r\n|\n))+")
_TRAILING_BLANK_LINES_RE = re.compile(r"(?:(?:\r\n|\n)[ \t]*)+\Z")


def normalize_chat_input(chat_input: str) -> str:
    """Remove chat-envelope blank lines and final horizontal whitespace."""

    value = _LEADING_BLANK_LINES_RE.sub("", chat_input)
    value = _TRAILING_BLANK_LINES_RE.sub("", value)
    return value.rstrip(" \t")


def parse_chat_input(chat_input: str) -> ChatInput:
    """Parse one complete terminal-chat input."""

    body = normalize_chat_input(chat_input)
    if not body:
        raise ValueError("chat input is empty")
    if body == ":?":
        return RunOverrideHelp()

    first, separator, rest = body.partition("\n")
    first = first.removesuffix("\r")
    quick = _parse_slash(first)
    if quick is not None:
        if separator and rest.strip(" \t\r\n"):
            raise ValueError("quick command cannot be combined with other input")
        return quick

    override, named, primary_source = parse_policy_prefix(body)
    if primary_source.startswith("/") and not primary_source.startswith("//"):
        combined = _parse_slash(primary_source.splitlines()[0])
        if combined is not None:
            raise ValueError("slash command cannot be combined with other input")
    runnable_input = parse_input(primary_source or None, named=named)
    if runnable_input._ is None and not runnable_input.named:
        if not override.empty:
            raise ValueError("colon override requires runnable input")
        raise ValueError("chat input is empty")
    return override, runnable_input


def is_runnable_input(
    value: ChatInput,
) -> TypeGuard[tuple[RunOverride, RunnableInputRaw]]:
    """Return whether a chat input contains one runnable invocation."""

    return (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], RunOverride)
        and isinstance(value[1], RunnableInputRaw)
    )


def is_slash_input(value: str) -> bool:
    """Return whether normalized input is intended as a slash command."""

    return value.startswith("/") and not value.startswith("//")


def slash_command_name(value: str) -> str | None:
    """Return the structural slash name from one normalized input."""

    if not is_slash_input(value):
        return None
    first = value.splitlines()[0]
    name, _tail = _command_parts(first)
    return name


def _parse_slash(line: str) -> QuickCommand | None:
    if not line.startswith("/") or line.startswith("//"):
        return None
    name, tail = _command_parts(line)
    return QuickCommand(name=name, tail=tail)


def _command_parts(line: str) -> tuple[str, str | None]:
    head, separator, raw_tail = line.partition(" ")
    if not separator:
        head, _separator, raw_tail = line.partition("\t")
    return head.removeprefix("/"), raw_tail.strip(" \t") or None


__all__ = [
    "ChatInput",
    "QuickCommand",
    "RunOverrideHelp",
    "is_runnable_input",
    "is_slash_input",
    "normalize_chat_input",
    "parse_chat_input",
    "slash_command_name",
]
