from __future__ import annotations

import json
from pathlib import Path

from click import unstyle
import pytest
from typer.testing import CliRunner

from toolang.cli.caps.main import app as caps_app
from toolang.cli.toolang.main import app as toolang_app


runner = CliRunner()


def test_query_is_hidden_but_direct_help_explains_the_grammar() -> None:
    root = runner.invoke(toolang_app, ["--help"])
    hidden = runner.invoke(toolang_app, ["hidden"])
    query = runner.invoke(toolang_app, ["query", "--help"])
    bare = runner.invoke(toolang_app, ["query"])

    assert root.exit_code == 0, root.stderr
    assert "query" not in unstyle(root.stdout)
    assert hidden.exit_code == 0, hidden.stderr
    assert "query" in unstyle(hidden.stdout)
    assert query.exit_code == 0, query.stderr
    assert 'QUERY = MATCH ("," MATCH)*' in unstyle(query.stdout)
    assert "models, tools, psyches, skills, services, prompts" in unstyle(query.stdout)
    assert bare.exit_code == 0, bare.stderr
    assert 'QUERY = MATCH ("," MATCH)*' in unstyle(bare.stdout)


@pytest.mark.parametrize(
    ("collection", "identity"),
    [
        ("models", "provider/model"),
        ("tools", "toolset/tool"),
        ("psyches", "psyches/psyche"),
        ("skills", "skills/skill"),
        ("services", "services/service"),
        ("prompts", "prompts/prompt"),
    ],
)
def test_query_command_publishes_human_and_json_schema(
    collection: str,
    identity: str,
) -> None:
    human = runner.invoke(toolang_app, ["query", collection])
    machine = runner.invoke(toolang_app, ["query", collection, "--json"])

    assert human.exit_code == 0, human.stderr
    assert f"Collection: {collection}" in unstyle(human.stdout)
    assert f"Identity: {identity}" in unstyle(human.stdout)
    assert "Fields:" in unstyle(human.stdout)
    assert "Columns:" not in unstyle(human.stdout)
    assert machine.exit_code == 0, machine.stderr
    payload = json.loads(machine.stdout)
    assert payload["collection"] == collection
    assert payload["fields"]
    assert "columns" not in payload


@pytest.mark.parametrize("collection", ["caps", "model", "unknown"])
def test_query_command_rejects_non_base_collections(collection: str) -> None:
    result = runner.invoke(toolang_app, ["query", collection])

    assert result.exit_code == 2
    stderr = unstyle(result.stderr)
    assert "unknown query collection" in stderr
    assert "models, tools, psyches, skills, services, prompts" in stderr


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
    assert f"No such option: {removed_option}" in unstyle(result.stderr)


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
    assert "--query" not in unstyle(help_result.stdout)
    assert query_result.exit_code == 2
    assert "No such option: --query" in unstyle(query_result.stderr)


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
    assert f"No such option: {legacy_option}" in unstyle(result.stderr)


def test_tools_reports_invalid_queries_without_a_traceback(tmp_path: Path) -> None:
    result = runner.invoke(
        toolang_app,
        [
            "--root",
            str(tmp_path / "toolang"),
            "tools",
            "--query",
            "*[unknown=value]",
        ],
    )

    assert result.exit_code == 1
    assert "unknown tools query field 'unknown'" in unstyle(result.stderr)
    assert "Traceback" not in result.stderr


def test_each_query_enabled_list_points_to_query_help() -> None:
    commands = (
        (toolang_app, ["models", "--help"], "too query models"),
        (toolang_app, ["tools", "--help"], "too query tools"),
        (caps_app, ["skill", "list", "--help"], "too query skills"),
    )

    for app, command, expected in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.stderr
        output = " ".join(unstyle(result.stdout).split())
        _too, _query, collection = expected.split()
        assert "too query" in output
        assert f"{collection}'." in output


def test_allow_help_uses_collection_query_vocabulary() -> None:
    result = runner.invoke(toolang_app, ["run", "--help"])

    assert result.exit_code == 0, result.stderr
    output = unstyle(result.stdout)
    assert "COLLECTION=QUERY" in output
    assert "SELECTORS" not in output
