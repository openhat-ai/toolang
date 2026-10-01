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


@pytest.mark.parametrize(
    "command",
    [["adapters"], ["catalogs"], ["toolsets"], ["sandboxes"], ["channel", "list"]],
)
@pytest.mark.parametrize("option", ["--json", "--human"])
def test_plugin_inventories_only_support_default_tables(command, option):
    result = runner.invoke(toolang_app, [*command, option])
    assert result.exit_code == 2
    assert f"No such option: {option}" in strip_ansi(result.stderr)


@pytest.mark.parametrize(
    ("app", "command"),
    [
        (toolang_app, ["caps"]),
        (caps_app, ["list"]),
        (caps_app, ["skill", "list"]),
    ],
)
@pytest.mark.parametrize("relative_root", [False, True])
def test_cap_location_query_uses_the_same_root_in_allow_and_inspection(
    tmp_path, monkeypatch, app, command, relative_root
):
    tmp_path = tmp_path.resolve()
    monkeypatch.chdir(tmp_path.parent)
    skill = tmp_path / "skills" / "reviewer" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: reviewer\ndescription: Review changes\n---\nReview.\n")
    expression = f'*[location="{skill}"]'
    (tmp_path / "config.toml").write_text(
        f"[allow]\nskills = [{json.dumps(expression)}]\n"
    )
    result = runner.invoke(
        app,
        [
            "--root",
            tmp_path.name if relative_root else str(tmp_path),
            *command,
            "--query",
            expression,
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stderr
    records = json.loads(result.stdout)
    assert [record["ref"] for record in records] == ["skill/reviewer"]
    assert records[0]["location"] == str(skill)
    assert records[0]["tags"] == ["ready", "local", "root", "authored"]


def test_roaming_cap_location_uses_selected_layout_root(tmp_path, monkeypatch, capsys):
    from toolang.cli.toolang.main import main

    tmp_path = tmp_path.resolve()
    source = tmp_path / "demo.too"
    source.write_text("agic:\n  {{_}}\n")
    skill = tmp_path / ".toolang" / "skills" / "reviewer" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: reviewer\ndescription: Review changes\n---\nReview.\n")
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path / "unrelated-root"))

    result = main([str(source), "caps", "--json"])

    output = capsys.readouterr()
    assert result == 0, output.err
    records = json.loads(output.out)
    assert [record["ref"] for record in records] == ["skill/reviewer"]
    assert records[0]["location"] == str(skill)
