from __future__ import annotations

import logging
import logging.config
import re
import sys
from typing import cast

import toolang.up.logging as up_logging
from toolang.up.logging import (
    DEFAULT_AGENT_LOG_SPEC,
    HttpxLogFilter,
    LoggingPlan,
    MessageRegexFilter,
    build_uvicorn_log_config,
    configure_logging,
    configure_logging_plan,
    resolve_agent_logging,
)
from toolang.common.env_logger import (
    OFF_LOG_LEVEL,
    PY_LOG_ENV_VAR,
    parse_log_level,
    parse_log_spec,
    resolve_log_spec,
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
    assert args[3] == 200
    assert (
        record.getMessage()
        == 'HTTP Request: POST https://api.telegram.org/bot<redacted>/getUpdates "HTTP/1.1 200 OK"'
    )


def test_build_uvicorn_log_config_registers_httpx_filter_and_keeps_http_debug_off_by_default() -> (
    None
):
    config = build_uvicorn_log_config()

    filters = cast(dict[str, object], config["filters"])
    loggers = cast(dict[str, dict[str, object]], config["loggers"])
    handlers = cast(dict[str, dict[str, object]], config["handlers"])

    assert "httpx" in filters
    assert loggers["httpx"]["level"] == "OFF"
    assert loggers["httpcore"]["level"] == "OFF"
    assert handlers["default"]["level"] == "ERROR"


def test_build_uvicorn_log_config_keeps_http_debug_off_when_toolang_debug_is_enabled() -> (
    None
):
    config = build_uvicorn_log_config(
        spec=parse_log_spec("toolang=debug", default_root_level=logging.ERROR)
    )

    loggers = cast(dict[str, dict[str, object]], config["loggers"])
    handlers = cast(dict[str, dict[str, object]], config["handlers"])

    assert handlers["default"]["level"] == "DEBUG"
    assert loggers["httpx"]["level"] == "OFF"
    assert loggers["httpcore"]["level"] == "OFF"


def test_build_uvicorn_log_config_uses_formatter_stream_colors(monkeypatch) -> None:
    class Tty:
        def isatty(self) -> bool:
            return True

    class Plain:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stderr", Tty())
    monkeypatch.setattr(sys, "stdout", Plain())

    config = build_uvicorn_log_config()

    formatters = cast(dict[str, dict[str, object]], config["formatters"])
    assert formatters["default"]["use_colors"] is True
    assert formatters["access"]["use_colors"] is False


def test_parse_log_level_accepts_warn_alias() -> None:
    assert parse_log_level("warn") == logging.WARNING


def test_parse_log_spec_supports_directives_and_regex() -> None:
    spec = parse_log_spec(
        "info,toolang.execution=debug,httpx=off/hello",
        default_root_level=logging.INFO,
    )

    assert spec.root_level == logging.INFO
    assert spec.logger_levels == {
        "toolang.execution": logging.DEBUG,
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
        name="toolang.execution",
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

    configure_logging(spec="toolang.execution=debug", environ={})

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


def test_configure_logging_disables_colors_for_file_logs(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_dict_config(config: dict[str, object]) -> None:
        captured["config"] = config

    monkeypatch.setattr(logging.config, "dictConfig", fake_dict_config)

    configure_logging(
        spec="toolang.execution=debug", environ={}, log_path=tmp_path / "agent.log"
    )

    config = cast(dict[str, object], captured["config"])
    formatters = cast(dict[str, dict[str, object]], config["formatters"])
    assert formatters["default"]["use_colors"] is False
    assert formatters["access"]["use_colors"] is False


def test_resolve_agent_logging_defaults_run_and_start_to_agent_spec(tmp_path) -> None:
    agent_log_path = tmp_path / "agent.log"

    run_plan = resolve_agent_logging(
        mode="run", environ={}, agent_log_path=agent_log_path
    )
    start_plan = resolve_agent_logging(
        mode="start", environ={}, agent_log_path=agent_log_path
    )

    assert run_plan.destination == "stderr"
    assert run_plan.path is None
    assert run_plan.spec == DEFAULT_AGENT_LOG_SPEC
    assert run_plan.environ[PY_LOG_ENV_VAR] == DEFAULT_AGENT_LOG_SPEC
    assert start_plan.destination == "agent_log"
    assert start_plan.path == agent_log_path
    assert start_plan.spec == DEFAULT_AGENT_LOG_SPEC
    assert start_plan.environ[PY_LOG_ENV_VAR] == DEFAULT_AGENT_LOG_SPEC


def test_resolve_agent_logging_uses_run_log_only_when_script_py_log_is_set(
    tmp_path,
) -> None:
    run_log_path = tmp_path / "run.log"

    quiet_plan = resolve_agent_logging(
        mode="script",
        environ={},
        run_log_path=run_log_path,
    )
    verbose_plan = resolve_agent_logging(
        mode="script",
        environ={PY_LOG_ENV_VAR: "toolang.execution=debug"},
        run_log_path=run_log_path,
    )

    assert quiet_plan.destination == "none"
    assert quiet_plan.path is None
    assert quiet_plan.spec is None
    assert PY_LOG_ENV_VAR not in quiet_plan.environ
    assert verbose_plan.destination == "run_log"
    assert verbose_plan.path == run_log_path
    assert verbose_plan.spec == "toolang.execution=debug"
    assert verbose_plan.environ[PY_LOG_ENV_VAR] == "toolang.execution=debug"


def test_configure_logging_plan_disables_diagnostics_for_quiet_script(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_configure_logging(*, spec, environ, log_path=None) -> None:
        captured.update(spec=spec, environ=dict(environ), log_path=log_path)

    monkeypatch.setattr(up_logging, "configure_logging", fake_configure_logging)

    configure_logging_plan(
        LoggingPlan(
            spec=None,
            destination="none",
            path=None,
            environ={},
        )
    )

    assert captured == {"spec": "off", "environ": {}, "log_path": None}
