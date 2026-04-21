"""Shared logging helpers."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import logging.config
import re

from .log_spec import (
    LogSpec,
    directive_level_for,
    ensure_custom_levels,
    level_name,
    resolve_log_spec,
)

DEFAULT_LOG_LEVEL = "ERROR"
DEFAULT_AGENT_LOG_SPEC = "error,toolang.run=info,toolang.runtime=info,httpx=off,httpcore=off"
DEFAULT_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_FORMAT = "%(asctime)s %(levelprefix)s [%(name)s] %(message)s"
DEFAULT_ACCESS_LOG_FORMAT = (
    '%(asctime)s %(levelprefix)s [%(name)s] %(client_addr)s - "%(request_line)s" %(status_code)s'
)
_TELEGRAM_BOT_URL_PATTERN = re.compile(r"(https://api\.telegram\.org/bot)[^/]+")
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


def build_uvicorn_log_config(*, spec: LogSpec | None = None, level: str = DEFAULT_LOG_LEVEL) -> dict[str, object]:
    """Build one logging config shared by Uvicorn and runtime loggers."""

    ensure_custom_levels()
    log_spec = spec or resolve_log_spec(cli_value=level, environ={}, default=DEFAULT_LOG_LEVEL)
    root_level = level_name(log_spec.root_level)
    handler_level = level_name(log_spec.handler_level)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": DEFAULT_LOG_FORMAT,
                "datefmt": DEFAULT_LOG_DATE_FORMAT,
                "use_colors": None,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": DEFAULT_ACCESS_LOG_FORMAT,
                "datefmt": DEFAULT_LOG_DATE_FORMAT,
                "use_colors": None,
            },
        },
        "filters": {
            "httpx": {
                "()": "toolang.config.log.HttpxLogFilter",
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
                "level": _logger_level_name("uvicorn", log_spec, default=log_spec.root_level),
                "handlers": ["default"],
                "propagate": False,
            },
            "uvicorn.error": {
                "level": _logger_level_name("uvicorn.error", log_spec, default=log_spec.root_level),
                "handlers": ["default"],
                "propagate": False,
            },
            "uvicorn.access": {
                "level": _logger_level_name("uvicorn.access", log_spec, default=log_spec.root_level),
                "handlers": ["access"],
                "propagate": False,
            },
            "watchfiles.main": {
                "level": _logger_level_name("watchfiles.main", log_spec, default=logging.WARNING),
                "handlers": ["default"],
                "propagate": False,
            },
            "httpx": {
                "level": _logger_level_name("httpx", log_spec, default=logging.DEBUG),
                "handlers": ["default"],
                "filters": ["httpx"],
                "propagate": False,
            },
            "httpcore": {
                "level": _logger_level_name("httpcore", log_spec, default=logging.DEBUG),
                "handlers": ["default"],
                "filters": ["httpx"],
                "propagate": False,
            },
        },
    }


def configure_logging(*, spec: str | None, environ: Mapping[str, str] | None = None) -> None:
    """Install the shared Toolang logging config for one CLI/runtime process."""

    log_spec = resolve_log_spec(
        cli_value=spec,
        environ={} if environ is None else environ,
        default=DEFAULT_LOG_LEVEL,
    )
    logging.config.dictConfig(build_uvicorn_log_config(spec=log_spec))
    if log_spec.message_filter is not None:
        _install_message_filter(MessageRegexFilter(log_spec.message_filter))
    _install_logger_overrides(log_spec)


def _redact_httpx_arg(value: object) -> object:
    if isinstance(value, str):
        return _redact_telegram_bot_url(value)
    return value


def _redact_telegram_bot_url(text: str) -> str:
    return _TELEGRAM_BOT_URL_PATTERN.sub(r"\1<redacted>", text)


def _logger_level_name(target: str, spec: LogSpec, *, default: int) -> str:
    level = directive_level_for(target, spec.logger_levels)
    return level_name(default if level is None else level)


def _install_message_filter(filter_obj: MessageRegexFilter) -> None:
    seen: set[int] = set()
    root = logging.getLogger()
    for handler in root.handlers:
        if id(handler) in seen:
            continue
        handler.addFilter(filter_obj)
        seen.add(id(handler))
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "watchfiles.main", "httpx", "httpcore"):
        for handler in logging.getLogger(logger_name).handlers:
            if id(handler) in seen:
                continue
            handler.addFilter(filter_obj)
            seen.add(id(handler))


def _install_logger_overrides(spec: LogSpec) -> None:
    for target, level in spec.logger_levels.items():
        logging.getLogger(target).setLevel(level)
