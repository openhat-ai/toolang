"""Default logging for Toolang CLI and agent startup."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import logging.config
from pathlib import Path
import re
import sys
from typing import Any, Literal, cast

from toolang.common.env_logger import (
    LogSpec,
    OFF_LOG_LEVEL,
    PY_LOG_ENV_VAR,
    directive_level_for,
    ensure_custom_levels,
    level_name,
    resolve_log_spec,
)

DEFAULT_LOG_LEVEL = "ERROR"
DEFAULT_AGENT_LOG_SPEC = "error,toolang.up.server=info,toolang.state=info,toolang.execution=info,httpx=off,httpcore=off"
DEFAULT_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_FORMAT = "%(asctime)s %(levelprefix)s [%(name)s] %(message)s"
DEFAULT_ACCESS_LOG_FORMAT = '%(asctime)s %(levelprefix)s [%(name)s] %(client_addr)s - "%(request_line)s" %(status_code)s'
_TELEGRAM_BOT_URL_PATTERN = re.compile(r"(https://api\.telegram\.org/bot)[^/]+")
LogMode = Literal["run", "start", "script"]
LogDestination = Literal["stderr", "agent_log", "run_log", "none"]


@dataclass(frozen=True, slots=True)
class LoggingPlan:
    """Resolved logging behavior for one agent entrypoint."""

    spec: str | None
    destination: LogDestination
    path: Path | None
    environ: dict[str, str]


class HttpxLogFilter(logging.Filter):
    """Redact sensitive URL segments and demote noisy request logs to DEBUG."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith(("httpx", "httpcore")):
            return True
        record.msg = _redact_telegram_bot_url(str(record.msg))
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_httpx_arg(item) for item in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: _redact_httpx_arg(value) for key, value in record.args.items()
            }
        if record.levelno == logging.INFO:
            record.levelno = logging.DEBUG
            record.levelname = logging.getLevelName(logging.DEBUG)
        return True


class MessageRegexFilter(logging.Filter):
    """Filter records by one compiled regex against the formatted message."""

    def __init__(self, pattern: re.Pattern[str]) -> None:
        super().__init__()
        self._pattern = pattern

    def filter(self, record: logging.LogRecord) -> bool:
        return bool(self._pattern.search(record.getMessage()))


def build_uvicorn_log_config(
    *,
    spec: LogSpec | None = None,
    level: str = DEFAULT_LOG_LEVEL,
    default_use_colors: bool | None = None,
    access_use_colors: bool | None = None,
) -> dict[str, object]:
    """Build one logging config shared by Uvicorn and runtime loggers."""

    ensure_custom_levels()
    log_spec = spec or resolve_log_spec(
        cli_value=level, environ={}, default=DEFAULT_LOG_LEVEL
    )
    root_level = level_name(log_spec.root_level)
    handler_level = level_name(log_spec.handler_level)
    default_colors = (
        _stream_uses_colors(sys.stderr)
        if default_use_colors is None
        else default_use_colors
    )
    access_colors = (
        _stream_uses_colors(sys.stdout)
        if access_use_colors is None
        else access_use_colors
    )
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": DEFAULT_LOG_FORMAT,
                "datefmt": DEFAULT_LOG_DATE_FORMAT,
                "use_colors": default_colors,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": DEFAULT_ACCESS_LOG_FORMAT,
                "datefmt": DEFAULT_LOG_DATE_FORMAT,
                "use_colors": access_colors,
            },
        },
        "filters": {
            "httpx": {
                "()": "toolang.up.logging.HttpxLogFilter",
            }
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "level": handler_level,
                "stream": "ext://sys.stderr",
            },
            "access": {
                "class": "logging.StreamHandler",
                "formatter": "access",
                "level": handler_level,
                "stream": "ext://sys.stdout",
            },
        },
        "root": {
            "level": root_level,
            "handlers": ["default"],
        },
        "loggers": {
            "uvicorn": {
                "level": _logger_level_name(
                    "uvicorn", log_spec, default=log_spec.root_level
                ),
                "handlers": ["default"],
                "propagate": False,
            },
            "uvicorn.error": {
                "level": _logger_level_name(
                    "uvicorn.error", log_spec, default=log_spec.root_level
                ),
                "handlers": ["default"],
                "propagate": False,
            },
            "uvicorn.access": {
                "level": _logger_level_name(
                    "uvicorn.access", log_spec, default=log_spec.root_level
                ),
                "handlers": ["access"],
                "propagate": False,
            },
            "watchfiles.main": {
                "level": _logger_level_name(
                    "watchfiles.main", log_spec, default=logging.WARNING
                ),
                "handlers": ["default"],
                "propagate": False,
            },
            "httpx": {
                "level": _logger_level_name("httpx", log_spec, default=OFF_LOG_LEVEL),
                "handlers": ["default"],
                "filters": ["httpx"],
                "propagate": False,
            },
            "httpcore": {
                "level": _logger_level_name(
                    "httpcore", log_spec, default=OFF_LOG_LEVEL
                ),
                "handlers": ["default"],
                "filters": ["httpx"],
                "propagate": False,
            },
        },
    }


def configure_logging(
    *,
    spec: str | None,
    environ: Mapping[str, str] | None = None,
    log_path: Path | None = None,
) -> None:
    """Install the shared Toolang logging config for one CLI/runtime process."""

    log_spec = resolve_log_spec(
        cli_value=spec,
        environ={} if environ is None else environ,
        default=DEFAULT_LOG_LEVEL,
    )
    config = build_uvicorn_log_config(
        spec=log_spec,
        default_use_colors=False if log_path is not None else None,
        access_use_colors=False if log_path is not None else None,
    )
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers = cast(dict[str, object], config.get("handlers", {}))
        for value in handlers.values():
            if isinstance(value, dict):
                handler = cast(dict[str, Any], value)
                handler.pop("stream", None)
                handler["class"] = "logging.FileHandler"
                handler["filename"] = str(log_path)
                handler["encoding"] = "utf-8"
    logging.config.dictConfig(config)
    if log_spec.message_filter is not None:
        _install_message_filter(MessageRegexFilter(log_spec.message_filter))
    _install_logger_overrides(log_spec)


def resolve_agent_logging(
    *,
    mode: LogMode,
    environ: Mapping[str, str],
    agent_log_path: Path | None = None,
    run_log_path: Path | None = None,
) -> LoggingPlan:
    """Resolve logging spec, destination, and environment for one agent entrypoint."""

    resolved_environ = dict(environ)
    spec = resolved_environ.get(PY_LOG_ENV_VAR, "").strip()
    if mode in {"run", "start"}:
        if not spec:
            spec = DEFAULT_AGENT_LOG_SPEC
            resolved_environ[PY_LOG_ENV_VAR] = spec
        if mode == "run":
            return LoggingPlan(
                spec=spec, destination="stderr", path=None, environ=resolved_environ
            )
        if agent_log_path is None:
            raise ValueError("agent_log path is required for start logging")
        return LoggingPlan(
            spec=spec,
            destination="agent_log",
            path=agent_log_path,
            environ=resolved_environ,
        )
    if spec:
        if run_log_path is None:
            raise ValueError("run_log path is required for script logging")
        return LoggingPlan(
            spec=spec,
            destination="run_log",
            path=run_log_path,
            environ=resolved_environ,
        )
    resolved_environ.pop(PY_LOG_ENV_VAR, None)
    return LoggingPlan(
        spec=None, destination="none", path=None, environ=resolved_environ
    )


def configure_logging_plan(plan: LoggingPlan) -> None:
    """Install logging for one resolved logging plan in the current process."""

    if plan.destination == "none":
        configure_logging(spec="off", environ={})
        return
    log_path = plan.path if plan.destination in {"agent_log", "run_log"} else None
    configure_logging(spec=plan.spec, environ=plan.environ, log_path=log_path)


def _redact_httpx_arg(value: object) -> object:
    if isinstance(value, str):
        return _redact_telegram_bot_url(value)
    return value


def _redact_telegram_bot_url(text: str) -> str:
    return _TELEGRAM_BOT_URL_PATTERN.sub(r"\1<redacted>", text)


def _logger_level_name(target: str, spec: LogSpec, *, default: int) -> str:
    level = directive_level_for(target, spec.logger_levels)
    return level_name(default if level is None else level)


def _stream_uses_colors(stream: object) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def _install_message_filter(filter_obj: MessageRegexFilter) -> None:
    seen: set[int] = set()
    root = logging.getLogger()
    for handler in root.handlers:
        if id(handler) in seen:
            continue
        handler.addFilter(filter_obj)
        seen.add(id(handler))
    for logger_name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "watchfiles.main",
        "httpx",
        "httpcore",
    ):
        for handler in logging.getLogger(logger_name).handlers:
            if id(handler) in seen:
                continue
            handler.addFilter(filter_obj)
            seen.add(id(handler))


def _install_logger_overrides(spec: LogSpec) -> None:
    for target, level in spec.logger_levels.items():
        logging.getLogger(target).setLevel(level)
