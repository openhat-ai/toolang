from __future__ import annotations

import logging
from typing import cast

from toolang.experiments.config.log import (
    HttpxLogFilter,
    build_uvicorn_log_config,
)


def test_httpx_log_filter_redacts_telegram_token_and_demotes_to_debug() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=(
            "POST",
            "https://api.telegram.org/bot-secret-token/getUpdates",
            "HTTP/1.1",
            200,
            "OK",
        ),
        exc_info=None,
    )

    allowed = HttpxLogFilter().filter(record)

    assert allowed is True
    assert record.levelno == logging.DEBUG
    assert record.levelname == "DEBUG"
    args = cast(tuple[object, ...], record.args)
    assert args[1] == "https://api.telegram.org/bot<redacted>/getUpdates"


def test_build_uvicorn_log_config_registers_httpx_filter_and_debug_logger() -> None:
    config = build_uvicorn_log_config()

    filters = cast(dict[str, object], config["filters"])
    loggers = cast(dict[str, dict[str, object]], config["loggers"])
    handlers = cast(dict[str, dict[str, object]], config["handlers"])

    assert "httpx" in filters
    assert loggers["httpx"]["level"] == "DEBUG"
    assert loggers["httpcore"]["level"] == "DEBUG"
    assert handlers["default"]["level"] == "INFO"
