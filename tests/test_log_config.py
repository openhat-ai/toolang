from __future__ import annotations

import logging
import logging.config
import re
from typing import cast

from toolang.config.log import (
    HttpxLogFilter,
    MessageRegexFilter,
    build_uvicorn_log_config,
    configure_logging,
)
from toolang.config.log_spec import OFF_LOG_LEVEL, PY_LOG_ENV_VAR, parse_log_level, parse_log_spec, resolve_log_spec


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
    assert args[3] == 200
    assert record.getMessage() == 'HTTP Request: POST https://api.telegram.org/bot<redacted>/getUpdates "HTTP/1.1 200 OK"'


def test_build_uvicorn_log_config_registers_httpx_filter_and_debug_logger() -> None:
    config = build_uvicorn_log_config()

    filters = cast(dict[str, object], config["filters"])
    loggers = cast(dict[str, dict[str, object]], config["loggers"])
    handlers = cast(dict[str, dict[str, object]], config["handlers"])

    assert "httpx" in filters
    assert loggers["httpx"]["level"] == "DEBUG"
    assert loggers["httpcore"]["level"] == "DEBUG"
    assert handlers["default"]["level"] == "ERROR"


def test_parse_log_level_accepts_warn_alias() -> None:
    assert parse_log_level("warn") == logging.WARNING


def test_parse_log_spec_supports_directives_and_regex() -> None:
    spec = parse_log_spec(
        "info,toolang.runner=debug,httpx=off/hello",
        default_root_level=logging.INFO,
    )

    assert spec.root_level == logging.INFO
    assert spec.logger_levels == {
        "toolang.runner": logging.DEBUG,
        "httpx": OFF_LOG_LEVEL,
    }
    assert spec.handler_level == logging.DEBUG
    assert spec.message_filter is not None
    assert spec.message_filter.pattern == "hello"


def test_resolve_log_spec_uses_py_log_when_cli_missing() -> None:
    spec = resolve_log_spec(
        cli_value=None,
        environ={PY_LOG_ENV_VAR: "toolang=debug"},
        default="error",
    )

    assert spec.root_level == logging.ERROR
    assert spec.logger_levels == {"toolang": logging.DEBUG}


def test_message_regex_filter_matches_formatted_message() -> None:
    filter_obj = MessageRegexFilter(re.compile("200 OK"))
    record = logging.LogRecord(
        name="toolang.runner",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("POST", "https://api.openai.com/v1/responses", "HTTP/1.1", 200, "OK"),
        exc_info=None,
    )

    assert filter_obj.filter(record) is True


def test_configure_logging_installs_resolved_config(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_dict_config(config: dict[str, object]) -> None:
        captured["config"] = config

    monkeypatch.setattr(logging.config, "dictConfig", fake_dict_config)

    configure_logging(spec="toolang.runner=debug", environ={})

    config = cast(dict[str, object], captured["config"])
    handlers = cast(dict[str, dict[str, object]], config["handlers"])
    root = cast(dict[str, object], config["root"])
    assert handlers["default"]["level"] == "DEBUG"
    assert root["level"] == "ERROR"


def test_configure_logging_supports_off_level(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_dict_config(config: dict[str, object]) -> None:
        captured["config"] = config

    monkeypatch.setattr(logging.config, "dictConfig", fake_dict_config)

    configure_logging(spec="httpx=off", environ={})

    config = cast(dict[str, object], captured["config"])
    loggers = cast(dict[str, dict[str, object]], config["loggers"])
    assert loggers["httpx"]["level"] == "OFF"
