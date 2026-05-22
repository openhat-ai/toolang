"""Model selector parsing."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ModelSelector:
    """One parsed model selector."""

    raw: str
    pattern: str = "*"
    filters: dict[str, tuple[str, ...]] = field(default_factory=dict)


def split_model_selectors(items: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Split repeated and CSV model selector inputs."""

    values: list[str] = []
    for item in items or ():
        for value in _split_selector_csv(str(item)):
            text = value.strip()
            if text:
                values.append(text)
    return tuple(values)


def parse_model_selector(raw: str) -> ModelSelector:
    """Parse `namespace/name[filter]` selector syntax."""

    text = raw.strip()
    if not text:
        return ModelSelector(raw=raw)
    pattern = text
    filters_text = ""
    bracket_index = text.find("[")
    if bracket_index >= 0:
        if not text.endswith("]"):
            return ModelSelector(raw=raw, pattern=text)
        pattern = text[:bracket_index].strip() or "*"
        filters_text = text[bracket_index + 1 : -1].strip()
    filters: dict[str, list[str]] = {}
    for item in _split_filter_items(filters_text):
        key, value = _parse_filter_item(item)
        if key is None or value is None:
            continue
        filters.setdefault(key, []).append(value)
    return ModelSelector(
        raw=raw,
        pattern=pattern or "*",
        filters={key: tuple(values) for key, values in filters.items()},
    )


def _split_selector_csv(text: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(text):
        if char == "[":
            depth += 1
        elif char == "]" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return tuple(parts)


def _split_filter_items(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(item.strip() for item in text.split(",") if item.strip())


def _parse_filter_item(item: str) -> tuple[str | None, str | None]:
    key, sep, value = item.partition(":")
    if not sep:
        key, sep, value = item.partition("=")
    if sep:
        key = key.strip()
        value = value.strip()
        if key in {"streaming", "tools"}:
            value = _normalize_bool_filter(value)
        return (key or None, value or None)
    value = key.strip()
    if not value:
        return (None, None)
    if value in {"local", "remote"}:
        return ("scope", value)
    return ("provider", value)


def _normalize_bool_filter(value: str) -> str:
    text = value.strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return "true"
    if text in {"0", "false", "no", "n", "off"}:
        return "false"
    return text
