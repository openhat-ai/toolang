"""Syntax failures explain parser evidence without changing source validity."""

import pytest
from types import SimpleNamespace
from typing import cast

from tree_sitter import Node, Point

from toolang.lang import Program, format_source
from toolang.lang import cst
from toolang.lang.errors import ToolangFormatError, ToolangSyntaxError
from toolang.lang.diagnostics import syntax_message


@pytest.mark.parametrize(
    "source, explanation, line, column",
    [
        ("flow work(value: Text:\n  pass\n", "Expected ')'", 1, 22),
        ("struct X:\n  field:\n", "Expected a field type", 2, 9),
        ("flow work:\n  sort these items\n", "Malformed flow statement 'sort'", 2, 3),
        ("agic review:\n  user, broken\n", "Malformed message header 'user'", 2, 3),
        ("flow work(value: ):\n  run\n  repeat:\n", "parameter", 1, 16),
    ],
)
def test_parser_and_formatter_explain_and_locate_syntax(
    source, explanation, line, column
):
    for parse, error_type in (
        (Program.from_source, ToolangSyntaxError),
        (format_source, ToolangFormatError),
    ):
        with pytest.raises(error_type) as caught:
            parse(source)
        error = caught.value
        assert explanation in str(error)
        assert "Toolang 0.3 syntax" not in str(error)
        assert (error.line, error.column) == (line, column)
    messages = [
        item["message"]
        for item in cst.diagnostics(
            cst.parse(source.encode()).root_node, source.encode()
        )
    ]
    assert any(explanation in message for message in messages)


def test_missing_cap_value_keeps_semantic_details_but_explains_format_failure():
    source = "skill review:\n  description =\n  Review.\n"
    from toolang.lang.errors import ToolangValidationError

    with pytest.raises(ToolangValidationError, match="must be nonempty"):
        Program.from_source(source)
    with pytest.raises(ToolangFormatError, match="Expected a property value"):
        format_source(source)
    assert (
        "Expected a property value"
        in cst.diagnostics(cst.parse(source.encode()).root_node, source.encode())[0][
            "message"
        ]
    )


def test_recovery_error_does_not_blame_valid_run_syntax_for_missing_block():
    source = "flow work:\n  repeat 2 times:\n  run: Review.\n"
    with pytest.raises(ToolangFormatError) as caught:
        format_source(source)
    assert "block structure" in str(caught.value)
    assert "Malformed flow statement" not in str(caught.value)
    assert "run: Review." in str(caught.value)
    assert format_source(source.replace("  run:", "    run:"))


@pytest.mark.parametrize("header", ["flow work", "flow work:"])
def test_broad_error_reports_original_fragment_without_guessing_repair(header):
    with pytest.raises(ToolangFormatError) as caught:
        format_source(header)
    assert header in str(caught.value)
    assert "Unexpected syntax" in str(caught.value)
    assert "Expected ':'" not in str(caught.value)


def test_formatter_output_failure_has_generated_location(monkeypatch):
    monkeypatch.setattr(
        "toolang.lang.format._format_source_lines",
        lambda *args, **kwargs: ["# Generated", "flow broken"],
    )
    with pytest.raises(ToolangFormatError) as caught:
        format_source("agic:\n  pass\n")
    assert "Formatter produced invalid syntax" in str(caught.value)
    assert "generated line 2" in str(caught.value)
    assert caught.value.line is None
    assert isinstance(caught.value.__cause__, ToolangFormatError)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix", ["", "\ufeff", "#!/usr/bin/env too\n\n"])
def test_diagnostics_keep_original_positions_and_excerpts(newline, prefix):
    source = (prefix + "flow work(value: Text:\n  pass\n").replace("\n", newline)
    line = prefix.count("\n") + 1
    column = 25 if prefix == "\ufeff" else 22
    for parse, error in (
        (Program.from_source, ToolangSyntaxError),
        (format_source, ToolangFormatError),
    ):
        with pytest.raises(error) as caught:
            parse(source)
        assert (caught.value.line, caught.value.column) == (line, column)
        assert "flow work(value: Text:" in str(caught.value)
    data = cst.to_data(cst.parse(source.encode()), source)
    diagnostic = data["diagnostics"][0]
    assert diagnostic["start_point"] == {"row": line - 1, "column": column - 1}
    assert f"line {line}: Expected ')'" in diagnostic["message"]
    assert "flow work(value: Text:" in diagnostic["message"]


def test_missing_type_at_eof_keeps_authored_position():
    source = "struct X:\n\tfield:"
    for parse, error in (
        (Program.from_source, ToolangSyntaxError),
        (format_source, ToolangFormatError),
    ):
        with pytest.raises(error) as caught:
            parse(source)
        assert (caught.value.line, caught.value.column) == (2, 8)
        assert "Expected a field type" in str(caught.value)


def test_query_masking_does_not_leak_into_diagnostic_excerpt():
    source = "flow work:\n  sort {{#中文}}\n"
    with pytest.raises(ToolangFormatError) as caught:
        format_source(source)
    assert "{{#中文}}" in str(caught.value)
    assert "{{x中文}}" not in str(caught.value)


def test_error_selection_does_not_skip_an_earlier_generic_error():
    source = "@\nflow work(value: Text:\n  pass\n"
    with pytest.raises(ToolangSyntaxError) as caught:
        Program.from_source(source)
    assert caught.value.line == 1
    assert "'@'" in str(caught.value)
    assert "Expected ')'" not in str(caught.value)


def test_cst_keeps_overlapping_error_entries_and_native_ranges():
    source = "flow work(value: ):\n  run\n  repeat:\n"
    data = cst.to_data(cst.parse(source.encode()), source)
    assert [
        (item["kind"], item["node_type"], item["start_byte"], item["end_byte"])
        for item in data["diagnostics"]
    ] == [
        ("error", "ERROR", 0, 36),
        ("error", "ERROR", 15, 16),
        ("invalid", "invalid_flow_reserved_statement", 22, 26),
    ]


def test_long_excerpts_show_the_error_and_remain_bounded():
    source = f"flow work({'x' * 1000}: Text:\n  pass\n"
    with pytest.raises(ToolangFormatError) as caught:
        format_source(source)
    assert "Text:" in str(caught.value)
    assert len(str(caught.value)) < 250


@pytest.mark.parametrize("missing", [True, False])
def test_unknown_grammar_node_has_safe_single_line_fallback(missing):
    source = "中文 \x1b[31m unknown\n".encode()
    node = cast(
        Node,
        SimpleNamespace(
            type="future_token" if missing else "invalid_future_construct",
            is_missing=missing,
            parent=None,
            start_byte=7,
            start_point=Point(0, 7),
            end_point=Point(0, 7),
        ),
    )
    message = syntax_message(node, source)
    assert "in source" in message
    assert "unknown" in message
    assert "future_" not in message
    assert "\n" not in message and "\x1b" not in message
