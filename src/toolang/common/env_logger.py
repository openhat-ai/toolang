"""Parse env_logger-style logging directives."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import logging
import re

PY_LOG_ENV_VAR = "PY_LOG"
TRACE_LOG_LEVEL = 5
OFF_LOG_LEVEL = logging.CRITICAL + 10

_LEVEL_NAMES = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "TRACE": TRACE_LOG_LEVEL,
    "OFF": OFF_LOG_LEVEL,
}
_LEVEL_LABELS = {
    TRACE_LOG_LEVEL: "TRACE",
    OFF_LOG_LEVEL: "OFF",
}


@dataclass(frozen=True, slots=True)
class LogSpec:
    """One resolved logging spec for one process."""

    root_level: int
    logger_levels: dict[str, int]
    handler_level: int
    message_filter: re.Pattern[str] | None = None


def ensure_custom_levels() -> None:
    """Register custom logging level names once."""

    if logging.getLevelName(TRACE_LOG_LEVEL) != "TRACE":
        logging.addLevelName(TRACE_LOG_LEVEL, "TRACE")
    if logging.getLevelName(OFF_LOG_LEVEL) != "OFF":
        logging.addLevelName(OFF_LOG_LEVEL, "OFF")


def parse_log_spec(text: str, *, default_root_level: int) -> LogSpec:
    """Parse one env_logger-style directive string."""

    ensure_custom_levels()
    directive_text, message_filter = _split_message_filter(text)
    root_level = default_root_level
    logger_levels: dict[str, int] = {}
    for raw_directive in directive_text.split(","):
        directive = raw_directive.strip()
        if not directive:
            continue
        if "=" in directive:
            target_text, level_text = directive.split("=", 1)
            target = target_text.strip()
            if not target:
                raise ValueError(f"invalid log directive: {directive}")
            level = parse_log_level(level_text)
            logger_levels[target] = level
            continue
        if _looks_like_level(directive):
            root_level = parse_log_level(directive)
            continue
        logger_levels[directive] = TRACE_LOG_LEVEL
    handler_level = _handler_level(root_level, logger_levels.values())
    compiled_filter = re.compile(message_filter) if message_filter else None
    return LogSpec(
        root_level=root_level,
        logger_levels=logger_levels,
        handler_level=handler_level,
        message_filter=compiled_filter,
    )


def resolve_log_spec(
    *,
    cli_value: str | None,
    environ: Mapping[str, str],
    default: str,
) -> LogSpec:
    """Resolve one logging spec from CLI, env, or default."""

    text = cli_value if cli_value is not None else environ.get(PY_LOG_ENV_VAR)
    if text is None or not text.strip():
        text = default
    return parse_log_spec(text, default_root_level=parse_log_level(default))


def parse_log_level(text: str) -> int:
    """Parse one supported level name into a logging level number."""

    normalized = text.strip().upper()
    if normalized not in _LEVEL_NAMES:
        choices = ", ".join(sorted(_LEVEL_NAMES))
        raise ValueError(f"invalid log level: {text} (expected one of: {choices})")
    return _LEVEL_NAMES[normalized]


def level_name(level: int) -> str:
    """Return one stable label for one logging level."""

    if level in _LEVEL_LABELS:
        return _LEVEL_LABELS[level]
    return logging.getLevelName(level)


def directive_level_for(target: str, logger_levels: Mapping[str, int]) -> int | None:
    """Return the most specific matching directive level for one logger target."""

    current = target
    while current:
        if current in logger_levels:
            return logger_levels[current]
        current, _, _ = current.rpartition(".")
    return None


def _split_message_filter(text: str) -> tuple[str, str | None]:
    if "/" not in text:
        return text, None
    directives, regex = text.split("/", 1)
    regex = regex.strip()
    if not regex:
        raise ValueError(f"invalid log filter: {text}")
    return directives, regex


def _looks_like_level(text: str) -> bool:
    return text.strip().upper() in _LEVEL_NAMES


def _handler_level(root_level: int, logger_levels: Iterable[int]) -> int:
    levels = [root_level]
    for level in logger_levels:
        if isinstance(level, int) and level < OFF_LOG_LEVEL:
            levels.append(level)
    if not levels:
        return OFF_LOG_LEVEL
    return min(levels)
