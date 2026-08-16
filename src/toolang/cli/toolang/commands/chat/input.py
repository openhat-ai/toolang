"""Terminal-chat input normalization and interaction parsing."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TypeAlias, TypeGuard

from toolang.execution.policy import parse_policy_prefix
from toolang.execution.types import RunOverride
from toolang.lang.input import RunInputText, parse_input


@dataclass(frozen=True, slots=True)
class QuickCommand:
    """One immediate terminal-chat command."""

    name: str
    tail: str | None = None


ChatInput: TypeAlias = (
    QuickCommand
    | tuple[RunOverride, ...]
    | tuple[tuple[RunOverride, ...], RunInputText]
)

_POLICY_HEADS = frozenset(
    {
        "allow",
        "default",
        "limit",
        "model",
        "agic",
        "flow",
        "runnable",
        "models",
        "tools",
        "caps",
        "psyches",
        "skills",
        "services",
        "prompts",
    }
)
_QUICK_COMMANDS = frozenset(
    {
        "?",
        "agic",
        "exit",
        "flow",
        "help",
        "model",
        "models",
        "queue",
        "quit",
        "runnable",
        "show",
        "steer",
    }
)
_QUICK_WITHOUT_TAIL = frozenset(
    {"?", "agic", "exit", "flow", "help", "model", "models", "quit", "runnable"}
)
_QUICK_REQUIRING_TAIL = frozenset({"steer"})
_LEADING_BLANK_LINES_RE = re.compile(r"\A(?:[ \t]*(?:\r\n|\n))+")
_TRAILING_BLANK_LINES_RE = re.compile(r"(?:(?:\r\n|\n)[ \t]*)+\Z")


def normalize_chat_input(source: str) -> str:
    """Remove chat-envelope blank lines and final horizontal whitespace."""

    value = _LEADING_BLANK_LINES_RE.sub("", source)
    value = _TRAILING_BLANK_LINES_RE.sub("", value)
    return value.rstrip(" \t")


def parse_chat_input(source: str) -> ChatInput:
    """Parse one complete terminal-chat input."""

    body = normalize_chat_input(source)
    if not body:
        raise ValueError("chat input is empty")

    first, separator, rest = body.partition("\n")
    first = first.removesuffix("\r")
    quick = _parse_quick(first)
    if quick is not None:
        if separator and rest.strip(" \t\r\n"):
            raise ValueError("quick command cannot be combined with other input")
        return quick

    commands, named, primary_source = parse_policy_prefix(body)
    if commands and not named and not primary_source:
        return commands
    if primary_source.startswith(":") and not primary_source.startswith("::"):
        combined = _parse_quick(primary_source.splitlines()[0])
        if combined is not None:
            raise ValueError("quick command cannot be combined with other input")
    input_text = parse_input(primary_source or None, named=named)
    if input_text.primary is None and not input_text.named:
        raise ValueError("chat input is empty")
    return commands, input_text


def is_run_overrides(
    value: ChatInput,
) -> TypeGuard[tuple[RunOverride, ...]]:
    """Return whether a chat input is a policy-only command sequence."""

    return (
        bool(value) and isinstance(value, tuple) and isinstance(value[0], RunOverride)
    )


def is_run_input_text(
    value: ChatInput,
) -> TypeGuard[tuple[tuple[RunOverride, ...], RunInputText]]:
    """Return whether a chat input contains one runnable invocation."""

    return (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], tuple)
        and isinstance(value[1], RunInputText)
    )


def _parse_quick(line: str) -> QuickCommand | None:
    if not line.startswith(":") or line.startswith("::"):
        return None

    head, separator, raw_tail = line.partition(" ")
    if not separator:
        head, separator, raw_tail = line.partition("\t")
    name = head[1:]
    tail = raw_tail.strip(" \t") or None

    if name in _POLICY_HEADS and tail is not None:
        return None
    if name in {
        "allow",
        "default",
        "limit",
        "tools",
        "caps",
        "psyches",
        "skills",
        "services",
        "prompts",
    }:
        return None
    if name not in _QUICK_COMMANDS:
        raise ValueError(f"unknown command: :{name}")
    if name in _QUICK_WITHOUT_TAIL and tail is not None:
        raise ValueError(f":{name} does not accept an argument")
    if name in _QUICK_REQUIRING_TAIL and tail is None:
        raise ValueError(f":{name} requires an argument")
    return QuickCommand(name=name, tail=tail)


__all__ = [
    "ChatInput",
    "QuickCommand",
    "is_run_input_text",
    "is_run_overrides",
    "normalize_chat_input",
    "parse_chat_input",
]
