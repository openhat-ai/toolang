from __future__ import annotations

import json
from pathlib import Path

import pytest
from wcwidth import wcswidth
from typer._click.utils import strip_ansi
from typer.testing import CliRunner

from toolang.cli.toolang.main import app as toolang_app
from toolang.catalog import templates
from toolang.catalog.cap import AuthoredCaps, CapFile
from toolang.common.layout import place_edit_lock_path


runner = CliRunner()


@pytest.mark.parametrize("command", [("caps",), ("skill", "list")])
@pytest.mark.parametrize("width", [40, 80, 120, 240])
def test_cap_lists_keep_single_rows_and_complete_json(
    tmp_path, monkeypatch, command, width
):
    monkeypatch.setenv("COLUMNS", str(width))
    root = tmp_path / ("nested-source-" * 8)
    root.mkdir()
    description = "Complete workflow description. " * 12 + "Final instructions."
    source = templates.load_template("skill").raw_text.replace(
        "Trigger this skill for requests that need this workflow.", description
    )
    AuthoredCaps(root, lock_path=place_edit_lock_path(root)).create(
        CapFile.parse(source, kind="skill", name="review")
    )
    args = ["--root", str(root), *command, "--query", "skill/review"]
    before = runner.invoke(toolang_app, [*args, "--json"])
    human = runner.invoke(toolang_app, args)
    after = runner.invoke(toolang_app, [*args, "--json"])

    assert before.exit_code == human.exit_code == after.exit_code == 0
    assert json.loads(before.stdout) == json.loads(after.stdout)
    record = json.loads(before.stdout)[0]
    assert record["description"] == description.strip()
    assert record["location"].endswith("/skills/review/SKILL.md")
    lines = strip_ansi(human.stdout).splitlines()
    assert len(lines) == 5
    assert max(map(wcswidth, lines)) == max(80, width)
    assert lines[0].split() == ["CAP", "DESCRIPTION", "LOCATION", "STATUS"]
    assert lines[2].startswith("skill/review")
    assert lines[2].endswith("ready")
    assert str(root) not in lines[2]
    assert "local,root,authored" not in human.stdout
    assert "…" in lines[2]
    assert lines[-2] == ""
    assert lines[-1] == "1 " + ("cap" if command == ("caps",) else "skill")


@pytest.mark.parametrize(
    "command",
    [
        ("tools",),
        ("caps",),
        *((kind, "list") for kind in ("psyche", "skill", "service", "prompt")),
    ],
)
@pytest.mark.parametrize("force_color", [False, True])
def test_compact_resource_lists_do_not_expose_long(
    tmp_path, monkeypatch, command, force_color
):
    def reject_loading(*args, **kwargs):
        pytest.fail("Unknown options must be rejected before resource loading")

    monkeypatch.setattr("typer.rich_utils.FORCE_TERMINAL", force_color)
    monkeypatch.setenv("TERM", "xterm-256color")
    if force_color:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("toolang.cli.caps.commands._cap_entries", reject_loading)
    monkeypatch.setattr(
        "toolang.cli.toolang.commands.plugin.load_tool_setup", reject_loading
    )
    help_result = runner.invoke(toolang_app, [*command, "--help"], color=force_color)
    assert help_result.exit_code == 0
    assert "--long" not in strip_ansi(help_result.stdout)
    result = runner.invoke(
        toolang_app, ["--root", str(tmp_path), *command, "--long"], color=force_color
    )
    assert result.exit_code == 2
    assert ("\x1b[" in result.stderr) is force_color
    assert "No such option: --long" in strip_ansi(result.stderr)


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
        (toolang_app, ["caps"]),
        (toolang_app, ["skill", "list"]),
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
        (toolang_app, ["caps"], "--filter"),
        (toolang_app, ["skill", "list"], "--filter"),
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
        (toolang_app, ["caps"]),
        (toolang_app, ["skill", "list"]),
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


def test_allow_help_uses_key_value_and_topic_reference() -> None:
    result = runner.invoke(toolang_app, ["serve", "--help"])

    assert result.exit_code == 0, result.stderr
    output = strip_ansi(result.stdout)
    assert "<KEY>=<VALUE>" in output
    assert "'too help allow'" in " ".join(output.split())
    assert "Allow fields:" not in output
    assert "SELECTORS" not in output


@pytest.mark.parametrize(
    "command",
    [["adapters"], ["catalogs"], ["toolsets"], ["sandboxes"]],
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
        (toolang_app, ["skill", "list"]),
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
    assert [record["cap"] for record in records] == ["skill/reviewer"]
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
    assert [record["cap"] for record in records] == ["skill/reviewer"]
    assert records[0]["location"] == str(skill)
