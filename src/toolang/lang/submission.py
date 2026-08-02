"""Standalone parsing for textual submissions."""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex
from typing import Literal, TypeAlias

CommandKind: TypeAlias = Literal["model", "agic", "flow"]
Arguments: TypeAlias = tuple[tuple[str, str], ...]
InputContent: TypeAlias = str


@dataclass(frozen=True, slots=True)
class QuickCommand:
    """One immediate chat command."""

    name: str
    tail: str | None = None


@dataclass(frozen=True, slots=True)
class SettingCommand:
    """One setting change for later chat calls."""

    kind: CommandKind
    selector: str
    args: Arguments = ()


@dataclass(frozen=True, slots=True)
class RunOverride:
    """One selector override for a single runnable call."""

    kind: CommandKind
    selector: str
    args: Arguments = ()


@dataclass(frozen=True, slots=True)
class RunnableCall:
    """Unbound overrides and primary content for one runnable call."""

    overrides: tuple[RunOverride, ...]
    content: InputContent


Submission: TypeAlias = (
    QuickCommand | tuple[SettingCommand, ...] | RunnableCall
)

_ARGUMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SELECTOR_COMMANDS = frozenset({"model", "agic", "flow"})
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
        "show",
        "steer",
    }
)
_QUICK_WITHOUT_TAIL = frozenset(
    {"?", "agic", "exit", "flow", "help", "model", "models", "quit"}
)
_QUICK_REQUIRING_TAIL = frozenset({"steer"})


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    start: int
    end: int


def parse_submission(source: str) -> Submission:
    """Parse one complete submission without resolving external names."""

    body = _strip_final_line_break(source)
    if not body:
        raise ValueError("submission is empty")

    lines = _lines(body)
    first = lines[0]
    quick = _parse_quick(first.text)
    if quick is not None:
        if any(not _is_blank(line.text) for line in lines[1:]):
            raise ValueError("quick command cannot be combined with other input")
        return quick

    override_count = 0
    while (
        override_count < len(lines)
        and _is_selector_line(lines[override_count].text)
    ):
        override_count += 1

    if override_count == 0:
        _reject_command_prefix(first.text)
        _validate_content(body)
        return RunnableCall(overrides=(), content=body)

    remaining = lines[override_count:]
    if all(_is_blank(line.text) for line in remaining):
        settings = tuple(
            _parse_setting(line.text, line_number=index + 1)
            for index, line in enumerate(lines[:override_count])
        )
        _validate_command_kinds(
            tuple(setting.kind for setting in settings),
            subject="setting command",
        )
        return settings

    content_start = lines[override_count - 1].end
    if (
        override_count < len(lines)
        and _is_blank(lines[override_count].text)
    ):
        content_start = lines[override_count].end

    content = body[content_start:]
    _validate_content(content)
    overrides = tuple(
        _parse_override(line.text, line_number=index + 1)
        for index, line in enumerate(lines[:override_count])
    )
    _validate_command_kinds(
        tuple(override.kind for override in overrides),
        subject="run override",
    )
    return RunnableCall(overrides=overrides, content=content)


def parse_runnable_call(source: str) -> RunnableCall:
    """Parse a run-only submission, rejecting quick and setting commands."""

    submission = parse_submission(source)
    if not isinstance(submission, RunnableCall):
        raise ValueError("submission is not a runnable call")
    return submission


def _strip_final_line_break(source: str) -> str:
    if source.endswith("\r\n"):
        return source[:-2]
    if source.endswith("\n"):
        return source[:-1]
    return source


def _lines(source: str) -> tuple[_Line, ...]:
    lines: list[_Line] = []
    start = 0
    while start < len(source):
        newline = source.find("\n", start)
        if newline < 0:
            lines.append(_Line(source[start:], start, len(source)))
            break
        text_end = newline - 1 if source[newline - 1 : newline] == "\r" else newline
        lines.append(_Line(source[start:text_end], start, newline + 1))
        start = newline + 1
    return tuple(lines)


def _parse_quick(line: str) -> QuickCommand | None:
    if not line.startswith(":") or line.startswith("::"):
        return None

    head, separator, raw_tail = line.partition(" ")
    if not separator:
        head, separator, raw_tail = line.partition("\t")
    name = head[1:]
    tail = raw_tail.strip(" \t") or None

    if name in _SELECTOR_COMMANDS and tail is not None:
        return None
    if name not in _QUICK_COMMANDS:
        raise ValueError(f"unknown command: :{name}")
    if name in _QUICK_WITHOUT_TAIL and tail is not None:
        raise ValueError(f":{name} does not accept an argument")
    if name in _QUICK_REQUIRING_TAIL and tail is None:
        raise ValueError(f":{name} requires an argument")
    return QuickCommand(name=name, tail=tail)


def _is_selector_line(line: str) -> bool:
    if not line.startswith(":") or line.startswith("::"):
        return False
    head = line[1:].split(maxsplit=1)[0]
    return head in _SELECTOR_COMMANDS


def _parse_setting(line: str, *, line_number: int) -> SettingCommand:
    kind, selector, args = _parse_selector_fields(line, line_number=line_number)
    return SettingCommand(kind=kind, selector=selector, args=args)


def _parse_override(line: str, *, line_number: int) -> RunOverride:
    kind, selector, args = _parse_selector_fields(line, line_number=line_number)
    return RunOverride(kind=kind, selector=selector, args=args)


def _parse_selector_fields(
    line: str,
    *,
    line_number: int,
) -> tuple[CommandKind, str, Arguments]:
    try:
        tokens = shlex.split(line, comments=False, posix=True)
    except ValueError as error:
        raise ValueError(f"line {line_number}: {error}") from error

    if len(tokens) < 2:
        raise ValueError(f"line {line_number}: selector is required")

    kind = tokens[0][1:]
    if kind not in _SELECTOR_COMMANDS:
        raise ValueError(f"line {line_number}: unknown selector command")
    selector = tokens[1]
    if not selector:
        raise ValueError(f"line {line_number}: selector is empty")

    if kind == "model":
        if len(tokens) != 2:
            raise ValueError(f"line {line_number}: :model accepts no arguments")
        return "model", selector, ()

    if kind == "flow" and selector == "auto":
        raise ValueError(f"line {line_number}: :flow auto is not supported")
    if kind == "agic" and selector == "auto" and len(tokens) != 2:
        raise ValueError(f"line {line_number}: :agic auto accepts no arguments")

    args = _parse_arguments(tokens[2:], line_number=line_number)
    return kind, selector, args  # type: ignore[return-value]


def _parse_arguments(tokens: list[str], *, line_number: int) -> Arguments:
    args: list[tuple[str, str]] = []
    names: set[str] = set()
    for token in tokens:
        name, separator, value = token.partition("=")
        if not separator or not _ARGUMENT_NAME_RE.fullmatch(name):
            raise ValueError(
                f"line {line_number}: argument must use name=value syntax"
            )
        if name in names:
            raise ValueError(f"line {line_number}: duplicate argument: {name}")
        names.add(name)
        args.append((name, value))
    return tuple(args)


def _validate_command_kinds(
    kinds: tuple[CommandKind, ...],
    *,
    subject: str,
) -> None:
    if kinds.count("model") > 1:
        raise ValueError(f"duplicate model {subject}")
    if sum(kind in {"agic", "flow"} for kind in kinds) > 1:
        raise ValueError(f"duplicate runnable {subject}")


def _validate_content(content: str) -> None:
    if not content.strip():
        raise ValueError("input content is empty")
    for line in _lines(content):
        if _is_blank(line.text):
            continue
        _reject_command_prefix(line.text)
        return


def _reject_command_prefix(line: str) -> None:
    if line.startswith(":") and not line.startswith("::"):
        raise ValueError("input content must escape a leading colon as ::")


def _is_blank(line: str) -> bool:
    return not line.strip(" \t")


__all__ = [
    "Arguments",
    "CommandKind",
    "InputContent",
    "QuickCommand",
    "RunnableCall",
    "RunOverride",
    "SettingCommand",
    "Submission",
    "parse_runnable_call",
    "parse_submission",
]
