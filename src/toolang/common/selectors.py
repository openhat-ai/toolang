"""Shared selector-list parsing and matching helpers."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from types import MappingProxyType
from typing import Literal, TypeVar

from .errors import ToolangError

SelectorDomain = Literal["model", "tool", "cap"]
SelectorOperator = Literal["=", "+=", "-="]
_T = TypeVar("_T")

_IDENTITY_FILTER_KEYS = frozenset({"family", "kind", "name", "namespace", "ref"})
_ALLOWED_FILTER_KEYS: dict[SelectorDomain, frozenset[str]] = {
    "model": frozenset(
        {
            "provider",
            "adapter",
            "scope",
            "tools",
            "tool_call",
            "streaming",
            "alias",
            "tag",
            "family",
            "reasoning",
            "temperature",
            "structured_output",
            "attachment",
            "open_weights",
            "modalities.input",
            "modalities.output",
            "status",
            "available",
            "availability",
        }
    ),
    "tool": frozenset({"plugin"}),
    "cap": frozenset({"scope", "form", "origin"}),
}
_MODEL_SHORTHANDS = {
    "local": ("scope", "local"),
    "remote": ("scope", "remote"),
    "tools": ("tool_call", "true"),
    "streaming": ("streaming", "true"),
}
_CAP_SHORTHANDS = {
    "root": ("scope", "root"),
    "home": ("scope", "home"),
    "here": ("scope", "here"),
    "inline": ("form", "inline"),
    "ref": ("form", "ref"),
    "wired": ("form", "wired"),
    "file": ("form", "file"),
    "local": ("origin", "local"),
    "remote": ("origin", "remote"),
}


@dataclass(frozen=True, slots=True)
class Selector:
    """One parsed selector."""

    raw: str
    pattern: str = "*"
    filters: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "filters",
            MappingProxyType(
                {key: tuple(values) for key, values in self.filters.items()}
            ),
        )


def split_selector_list(items: Sequence[str] | None) -> tuple[str, ...]:
    """Split repeated and CSV selector-list inputs."""

    values: list[str] = []
    for item in items or ():
        for value in _split_selector_csv(str(item)):
            text = value.strip()
            if text:
                values.append(text)
    return tuple(values)


def parse_selector(
    raw: str,
    *,
    domain: SelectorDomain,
    implicit_family: str | None = None,
) -> Selector:
    """Parse one selector in a domain."""

    text = raw.strip()
    if not text:
        return Selector(raw=raw)
    pattern = text
    filters_text = ""
    bracket_index = text.find("[")
    if bracket_index >= 0:
        if text.count("[") != 1 or text.count("]") != 1 or not text.endswith("]"):
            raise ToolangError(f"invalid selector: {raw}")
        pattern = text[:bracket_index].strip() or "*"
        filters_text = text[bracket_index + 1 : -1].strip()
        if not filters_text:
            raise ToolangError(f"selector filter list cannot be empty: {raw}")
    elif "]" in text:
        raise ToolangError(f"invalid selector: {raw}")
    if implicit_family is not None and "/" in pattern.strip("* "):
        raise ToolangError(
            f"{domain} selector must not include a family in this context: {raw}"
        )
    filters: dict[str, list[str]] = {}
    for item in _split_filter_items(filters_text):
        key, value = _parse_filter_item(item, domain=domain)
        filters.setdefault(key, []).append(value)
    return Selector(
        raw=raw,
        pattern=pattern or "*",
        filters={key: tuple(values) for key, values in filters.items()},
    )


def selector_identity_matches(
    *,
    family: str,
    name: str,
    selector: Selector,
    extra_values: Sequence[str] = (),
) -> bool:
    """Return whether a selector pattern matches one family/name identity."""

    pattern = selector.pattern.strip() or "*"
    if pattern == "*":
        return True
    if "/" in pattern:
        family_pattern, _, name_pattern = pattern.partition("/")
        return fnmatchcase(family, family_pattern or "*") and fnmatchcase(
            name, name_pattern or "*"
        )
    del family
    values = (name, *extra_values)
    return any(
        value == pattern or fnmatchcase(value, pattern) for value in values if value
    )


def filter_value_matches(actual: str, allowed: Sequence[str]) -> bool:
    """Return whether one actual filter value matches any allowed values."""

    return any(actual == value or fnmatchcase(actual, value) for value in allowed)


def apply_selector_operations(
    base: Sequence[_T],
    operations: Sequence[tuple[SelectorOperator, tuple[str, ...]]],
    match: Callable[[tuple[str, ...]], Sequence[_T]],
    *,
    identity: Callable[[_T], Hashable] = lambda item: item,
) -> tuple[_T, ...]:
    """Apply ordered selector-list operations within one immutable base set."""

    inherited: list[_T] = []
    seen: set[Hashable] = set()
    for item in base:
        key = identity(item)
        if key not in seen:
            inherited.append(item)
            seen.add(key)
    current = list(inherited)
    for operator, selectors in operations:
        matches = list(match(selectors))
        if operator == "=":
            allowed = {identity(item) for item in matches}
            current = [item for item in current if identity(item) in allowed]
        elif operator == "+=":
            seen = {identity(item) for item in current}
            for item in matches:
                key = identity(item)
                if key not in seen:
                    current.append(item)
                    seen.add(key)
        else:
            blocked = {identity(item) for item in matches}
            current = [item for item in current if identity(item) not in blocked]
    return tuple(current)


def _split_selector_csv(text: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(text):
        if char == "[":
            if depth:
                raise ToolangError(f"invalid selector list: {text}")
            depth += 1
        elif char == "]":
            if not depth:
                raise ToolangError(f"invalid selector list: {text}")
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    if depth:
        raise ToolangError(f"invalid selector list: {text}")
    parts.append(text[start:])
    return tuple(parts)


def _split_filter_items(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(item.strip() for item in text.split(",") if item.strip())


def _parse_filter_item(item: str, *, domain: SelectorDomain) -> tuple[str, str]:
    key, sep, value = item.partition(":")
    if not sep:
        key, sep, value = item.partition("=")
    if sep:
        normalized_key = key.strip().lower()
        normalized_value = value.strip()
        if not normalized_key or not normalized_value:
            raise ToolangError(f"invalid selector filter: {item}")
        _validate_filter_key(normalized_key, domain=domain)
        return (
            normalized_key,
            _normalize_filter_value(normalized_key, normalized_value),
        )
    shorthand = key.strip()
    if not shorthand:
        raise ToolangError(f"invalid selector filter: {item}")
    normalized = _normalize_shorthand(shorthand, domain=domain)
    if normalized is None:
        raise ToolangError(f"unknown {domain} selector shorthand: {shorthand}")
    return normalized


def _validate_filter_key(key: str, *, domain: SelectorDomain) -> None:
    if key in _IDENTITY_FILTER_KEYS and not (domain == "model" and key == "family"):
        raise ToolangError(
            f"selector identity belongs in the pattern, not filter {key!r}"
        )
    if key not in _ALLOWED_FILTER_KEYS[domain]:
        allowed = ", ".join(sorted(_ALLOWED_FILTER_KEYS[domain]))
        raise ToolangError(
            f"unknown {domain} selector filter {key!r}; expected one of {allowed}"
        )


def _normalize_shorthand(
    item: str, *, domain: SelectorDomain
) -> tuple[str, str] | None:
    text = item.strip()
    lower = text.lower()
    if domain == "model":
        if lower in _MODEL_SHORTHANDS:
            key, value = _MODEL_SHORTHANDS[lower]
            return (key, value)
        return ("provider", text)
    if domain == "cap":
        return _CAP_SHORTHANDS.get(lower)
    return None


def _normalize_filter_value(key: str, value: str) -> str:
    if key in {
        "streaming",
        "tools",
        "tool_call",
        "reasoning",
        "temperature",
        "structured_output",
        "attachment",
        "open_weights",
        "available",
        "availability",
    }:
        return _normalize_bool_filter(value)
    return value


def _normalize_bool_filter(value: str) -> str:
    text = value.strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return "true"
    if text in {"0", "false", "no", "n", "off"}:
        return "false"
    raise ToolangError(f"invalid boolean selector filter: {value!r}")
