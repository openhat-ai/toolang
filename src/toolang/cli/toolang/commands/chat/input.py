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


ChatInput: TypeAlias = (
    QuickCommand
    | tuple[RunOverride, ...]
    | tuple[tuple[RunOverride, ...], RunnableInputRaw]
)

_QUICK_COMMANDS = frozenset(
    {
        "?",
        "agic",
        "exit",
        "flow",
        "help",
        "model",
        "q",
        "queue",
        "quit",
        "runnable",
        "s",
        "show",
        "steer",
    }
)
_QUICK_WITHOUT_TAIL = frozenset({"?", "exit", "help", "quit"})
_QUICK_REQUIRING_TAIL = frozenset({"s", "steer"})
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
    quick = _parse_slash(first)
    if quick is not None:
        if separator and rest.strip(" \t\r\n"):
            raise ValueError("quick command cannot be combined with other input")
        return quick

    commands, named, primary_source = parse_policy_prefix(body)
    if commands and not named and not primary_source:
        return commands
    if primary_source.startswith("/") and not primary_source.startswith("//"):
        combined = _parse_slash(primary_source.splitlines()[0])
        if combined is not None:
            raise ValueError("slash command cannot be combined with other input")
    runnable_input = parse_input(primary_source or None, named=named)
    if runnable_input.primary is None and not runnable_input.named:
        raise ValueError("chat input is empty")
    return commands, runnable_input


def is_run_overrides(
    value: ChatInput,
) -> TypeGuard[tuple[RunOverride, ...]]:
    """Return whether a chat input is a policy-only command sequence."""

    return (
        bool(value) and isinstance(value, tuple) and isinstance(value[0], RunOverride)
    )


def is_runnable_input(
    value: ChatInput,
) -> TypeGuard[tuple[tuple[RunOverride, ...], RunnableInputRaw]]:
    """Return whether a chat input contains one runnable invocation."""

    return (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], tuple)
        and isinstance(value[1], RunnableInputRaw)
    )


def _parse_slash(line: str) -> QuickCommand | None:
    if not line.startswith("/") or line.startswith("//"):
        return None
    name, tail = _command_parts(line)
    if name not in _QUICK_COMMANDS:
        raise ValueError(f"unknown command: /{name}")
    _validate_quick(name, tail)
    return QuickCommand(name=name, tail=tail)


def _command_parts(line: str) -> tuple[str, str | None]:
    head, separator, raw_tail = line.partition(" ")
    if not separator:
        head, _separator, raw_tail = line.partition("\t")
    return head.removeprefix("/"), raw_tail.strip(" \t") or None


def _validate_quick(name: str, tail: str | None) -> None:
    if name in _QUICK_WITHOUT_TAIL and tail is not None:
        raise ValueError(f"/{name} does not accept an argument")
    if name in _QUICK_REQUIRING_TAIL and tail is None:
        raise ValueError(f"/{name} requires an argument")


__all__ = [
    "ChatInput",
    "QuickCommand",
    "is_runnable_input",
    "is_run_overrides",
    "normalize_chat_input",
    "parse_chat_input",
]
