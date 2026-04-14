"""Shared logging helpers."""

from __future__ import annotations

import logging
import re

DEFAULT_LOG_LEVEL = "INFO"
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


def build_uvicorn_log_config(*, level: str = DEFAULT_LOG_LEVEL) -> dict[str, object]:
    """Build one logging config shared by Uvicorn and runtime loggers."""

    normalized_level = level.upper()
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
                "()": "toolang.experiments.config.log.HttpxLogFilter",
            }
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "level": normalized_level,
                "stream": "ext://sys.stderr",
            },
            "access": {
                "class": "logging.StreamHandler",
                "formatter": "access",
                "level": normalized_level,
                "stream": "ext://sys.stdout",
            },
        },
        "root": {
            "level": normalized_level,
            "handlers": ["default"],
        },
        "loggers": {
            "uvicorn": {
                "level": normalized_level,
                "handlers": ["default"],
                "propagate": False,
            },
            "uvicorn.error": {
                "level": normalized_level,
                "handlers": ["default"],
                "propagate": False,
            },
            "uvicorn.access": {
                "level": normalized_level,
                "handlers": ["access"],
                "propagate": False,
            },
            "watchfiles.main": {
                "level": "WARNING",
                "handlers": ["default"],
                "propagate": False,
            },
            "httpx": {
                "level": "DEBUG",
                "handlers": ["default"],
                "filters": ["httpx"],
                "propagate": False,
            },
            "httpcore": {
                "level": "DEBUG",
                "handlers": ["default"],
                "filters": ["httpx"],
                "propagate": False,
            },
        },
    }
def _redact_httpx_arg(value: object) -> object:
    return _redact_telegram_bot_url(str(value))


def _redact_telegram_bot_url(text: str) -> str:
    return _TELEGRAM_BOT_URL_PATTERN.sub(r"\1<redacted>", text)
