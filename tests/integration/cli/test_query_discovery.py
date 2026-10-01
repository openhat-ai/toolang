from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer._click.utils import strip_ansi
from typer.testing import CliRunner

from toolang.cli.caps.main import app as caps_app
from toolang.cli.toolang.main import app as toolang_app


runner = CliRunner()


def test_query_command_is_removed() -> None:
    for command in (["query"], ["query", "models", "--json"]):
        result = runner.invoke(toolang_app, command)
        assert result.exit_code != 0
    more = runner.invoke(toolang_app, ["more"])
    assert more.exit_code == 0, more.stderr
    assert "query" not in strip_ansi(more.stdout)


@pytest.mark.parametrize(
    ("app", "command"),
    [
        (toolang_app, ["models"]),
        (toolang_app, ["tools"]),
        (caps_app, ["list"]),
        (caps_app, ["skill", "list"]),
    ],
)
@pytest.mark.parametrize("removed_option", ["--query-help", "--query-schema"])
def test_list_commands_reject_removed_query_discovery_options(
    app,
    command: list[str],
    removed_option: str,
) -> None:
    result = runner.invoke(app, [*command, removed_option])

    assert result.exit_code == 2
    assert f"No such option: {removed_option}" in strip_ansi(result.stderr)


@pytest.mark.parametrize(
    "command",
    [
        ["providers"],
        ["adapters"],
        ["catalogs"],
        ["toolsets"],
        ["sandboxes"],
        ["channel", "list"],
    ],
)
def test_diagnostic_and_plugin_lists_expose_no_query(command: list[str]) -> None:
    help_result = runner.invoke(toolang_app, [*command, "--help"])
    query_result = runner.invoke(toolang_app, [*command, "--query", "*"])

    assert help_result.exit_code == 0, help_result.stderr
    assert "--query" not in strip_ansi(help_result.stdout)
    assert query_result.exit_code == 2
    assert "No such option: --query" in strip_ansi(query_result.stderr)


@pytest.mark.parametrize(
    ("app", "command", "legacy_option"),
    [
        (toolang_app, ["models"], "--filter"),
        (toolang_app, ["tools"], "--filter"),
        (toolang_app, ["tools"], "--select"),
        (caps_app, ["list"], "--filter"),
        (caps_app, ["skill", "list"], "--filter"),
    ],
)
def test_query_enabled_commands_reject_legacy_query_options(
    app,
    command: list[str],
    legacy_option: str,
) -> None:
    result = runner.invoke(app, [*command, legacy_option, "*"])

    assert result.exit_code == 2
    assert f"No such option: {legacy_option}" in strip_ansi(result.stderr)


def test_tools_accepts_dynamic_fields_and_returns_empty_json(tmp_path: Path) -> None:
    result = runner.invoke(
        toolang_app,
        [
            "--root",
            str(tmp_path / "toolang"),
            "tools",
            "--query",
            "*[unknown=value]",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == []


@pytest.mark.parametrize(
    "app,command",
    [
        (toolang_app, ["models"]),
        (toolang_app, ["tools"]),
        (toolang_app, ["providers"]),
        (caps_app, ["list"]),
        (caps_app, ["skill", "list"]),
    ],
)
def test_resource_lists_offer_json_and_human_output(app, command) -> None:
    result = runner.invoke(app, [*command, "--help"])
    assert result.exit_code == 0, result.stderr
    output = strip_ansi(result.stdout)
    assert "--json" in output
    assert "--human" in output
    result = runner.invoke(app, [*command, "--human", "--json"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr


def test_allow_help_uses_resource_query_vocabulary() -> None:
    result = runner.invoke(toolang_app, ["serve", "--help"])

    assert result.exit_code == 0, result.stderr
    output = strip_ansi(result.stdout)
    assert "<RESOURCE>=<QUERY>" in output
    assert "SELECTORS" not in output
