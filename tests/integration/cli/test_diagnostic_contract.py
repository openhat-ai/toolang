"""Source diagnostics keep facts separate from their presentation."""

import pytest
from typer.testing import CliRunner

from toolang.cli.toolang.main import app


# Source, primary location, factual reason, original excerpt.
CASES = [
    (
        "\n\n\nagic issue(_: Text) sdf:\n  pass\n",
        "4:21",
        "Parse error in agic block",
        "sdf",
    ),
    (
        "flow work(value: Text:\n  pass\n",
        "1:22",
        "Expected ')' in parameter list",
        "flow work(value: Text:",
    ),
    ("struct X:\n  field:\n", "2:9", "Expected a field type", "field:"),
    (
        "flow work(value: ):\n  pass\n",
        "1:16",
        "Parse error in parameter list",
        ":",
    ),
    (
        "skill review:\n  description =\n  Review.\n",
        "2:16",
        "Property 'description' in skill 'review' must be nonempty",
        "description =",
    ),
    (
        "flow work:\n  sort these items\n",
        "2:1",
        "Parse error in flow block",
        "sort these items",
    ),
    (
        "agic review:\n  user, broken\n",
        "2:1",
        "Parse error in agic block",
        "user, broken",
    ),
    (
        "flow work:\n  repeat 2 times:\n  run: Review.\n",
        "3:1",
        "Expected an indented body in repeat block",
        "run: Review.",
    ),
    ("flow work", "1:1", "Parse error in flow block", "flow work"),
    ("flow work:", "1:11", "Expected an indented body in flow block", "flow work:"),
    ("@\n", "1:1", "Parse error", "@"),
    (
        "flow work:\n  sort {{#中文}}\n",
        "2:1",
        "Parse error in flow block",
        "sort {{#中文}}",
    ),
    ("@\nflow work(value: Text:\n  pass\n", "1:1", "Parse error", "@"),
    ("struct X:\n\tfield:", "2:8", "Expected a field type", "field:"),
]


@pytest.mark.parametrize("source,position,reason,excerpt", CASES)
@pytest.mark.parametrize(
    "command",
    [["parse"], ["parse", "--check"], ["fmt"], ["fmt", "--highlight", "--html"]],
)
def test_source_error_output_matrix(source, position, reason, excerpt, command):
    if command[0] == "fmt" and "description =" in source:
        reason = "Expected a property value after '='"
    result = CliRunner().invoke(
        app, [*command, "-", "--stdin-filepath", "case.too"], input=source
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"case.too:{position}: {reason}: {excerpt!r}\n"


@pytest.mark.parametrize("source,position,reason,excerpt", CASES)
def test_highlight_still_accepts_all_audited_sources(source, position, reason, excerpt):
    result = CliRunner().invoke(
        app, ["highlight", "-", "--color", "never"], input=source
    )
    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == source


@pytest.mark.parametrize("source,position,reason,excerpt", CASES)
@pytest.mark.parametrize("json_output", [False, True])
def test_cst_output_keeps_raw_entries_but_uses_the_same_rendering_contract(
    source, position, reason, excerpt, json_output
):
    import json

    if "description =" in source:
        reason = "Expected a property value after '='"
    if source == "struct X:\n\tfield:":
        position, reason, excerpt = "1:1", "Parse error in struct declaration", source
    expected = f"case.too:{position}: {reason}: {excerpt!r}\n"
    if any(
        text in source
        for text in ("sort these items", "sort {{#中文}}", "user, broken")
    ):
        context = "agic" if "user, broken" in source else "flow"
        expected += f"case.too:3:1: Expected an indented body in {context} block: ''\n"
    if source.startswith("@\nflow"):
        expected += (
            "case.too:2:22: Expected ')' in parameter list: 'flow work(value: Text:'\n"
        )
    command = ["parse", "--cst", *(["--json"] if json_output else [])]
    result = CliRunner().invoke(
        app, [*command, "-", "--stdin-filepath", "case.too"], input=source
    )
    assert result.exit_code == 1
    assert result.stderr == expected
    if json_output:
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == 1
        assert payload["source"] == source
        assert payload["diagnostics"][0]["message"] == reason
        assert set(payload["diagnostics"][0]) == {
            "kind",
            "node_type",
            "message",
            "start_byte",
            "end_byte",
            "start_point",
            "end_point",
        }
    else:
        assert result.stdout.startswith("(program")


def test_unlocated_validation_failure_has_no_fake_position_or_excerpt(monkeypatch):
    from toolang.base.errors import ToolangError

    def fail(program, *, external_runnables=None):
        raise ToolangError("Opaque failure at line 12.")

    monkeypatch.setattr("toolang.lang.validate._validate", fail)
    result = CliRunner().invoke(
        app,
        ["parse", "--check", "-", "--stdin-filepath", "case.too"],
        input="agic work:\n  pass\n",
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "case.too: Opaque failure at line 12.\n"
