"""Terminal-chat input normalization and interaction parsing."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal, TypeAlias, TypeGuard

from toolang.execution.policy import (
    COLON_COMMAND_NAMES,
    SLASH_COMMAND_NAMES,
    parse_policy_prefix,
)
from toolang.execution.types import RunOverride
from toolang.lang.input import CallInput, parse_input


@dataclass(frozen=True, slots=True)
class QuickCommand:
    """One immediate terminal-chat command."""

    name: str
    tail: str | None = None
    prefix: Literal["/", ":"] = "/"


ChatInput: TypeAlias = QuickCommand | tuple[RunOverride, CallInput[str]]

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
    first, separator, rest = body.partition("\n")
    first = first.removesuffix("\r")
    if is_command_input(first):
        if separator and rest.strip(" \t\r\n"):
            raise ValueError("command cannot be combined with other input")
        name, tail = _command_parts(first)
        if first.startswith(":") and name not in COLON_COMMAND_NAMES:
            raise ValueError(f"unknown colon command: :{name}")
        return QuickCommand(name, tail, ":" if first.startswith(":") else "/")

    override, call_input = parse_policy_prefix(body)
    runnable_input = parse_input(call_input)
    if not runnable_input and override.runnable is None:
        raise ValueError("chat input is empty")
    return override, runnable_input


def is_runnable_input(
    value: ChatInput,
) -> TypeGuard[tuple[RunOverride, CallInput[str]]]:
    """Return whether a chat input contains one runnable invocation."""

    return (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], RunOverride)
        and isinstance(value[1], CallInput)
    )


def is_command_input(value: str) -> bool:
    """Recognize standalone command intent without treating run directives as commands."""

    if value.startswith(":") and not value.startswith("::"):
        return True
    if not value.startswith("/") or value.startswith("//"):
        return False
    return _command_parts(value.splitlines()[0])[0] in SLASH_COMMAND_NAMES


def command_name(value: str) -> str | None:
    """Return the structural name of a standalone slash or colon command."""

    if not is_command_input(value):
        return None
    return _command_parts(value.splitlines()[0])[0]


def _command_parts(line: str) -> tuple[str, str | None]:
    head, *tail = line.split(maxsplit=1)
    return head[1:], tail[0].strip(" \t") or None if tail else None


__all__ = [
    "ChatInput",
    "QuickCommand",
    "command_name",
    "is_command_input",
    "is_runnable_input",
    "normalize_chat_input",
    "parse_chat_input",
]
