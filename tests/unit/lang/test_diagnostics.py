"""Syntax failures explain parser evidence without changing source validity."""

import pytest
from types import SimpleNamespace
from typing import cast

from tree_sitter import Node, Point

from toolang.lang import Program, format_source
from toolang.lang import cst
from toolang.lang.errors import ToolangFormatError, ToolangSyntaxError
from toolang.lang.diagnostics import (
    DiagnosticSource,
    render_diagnostic,
    source_position,
    syntax_diagnostic,
)


def rendered(error, source):
    return render_diagnostic(error.diagnostic, source=DiagnosticSource(source))


@pytest.mark.parametrize(
    "source, explanation, line, column",
    [
        ("flow work(value: Text:\n  pass\n", "Expected ')'", 1, 22),
        ("struct X:\n  field:\n", "Expected a field type", 2, 9),
        ("flow work:\n  sort these items\n", "Malformed flow statement 'sort'", 2, 3),
        ("agic review:\n  user, broken\n", "Malformed message header 'user'", 2, 3),
        ("flow work(value: ):\n  run\n  repeat:\n", "flow block", 1, 1),
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
    assert "Parse error in flow block" in str(caught.value)
    assert "Malformed flow statement" not in str(caught.value)
    assert "run: Review." in rendered(caught.value, source)
    assert (caught.value.line, caught.value.column) == (1, 1)
    assert caught.value.diagnostic.location is not None
    assert caught.value.diagnostic.location.precision == "recovery"
    assert format_source(source.replace("  run:", "    run:"))


@pytest.mark.parametrize("header", ["flow work", "flow work:"])
def test_broad_error_reports_original_fragment_without_guessing_repair(header):
    with pytest.raises(ToolangFormatError) as caught:
        format_source(header)
    assert header in rendered(caught.value, header)
    assert "Parse error" in str(caught.value)
    assert "Expected ':'" not in str(caught.value)


def test_formatter_output_failure_has_generated_location(monkeypatch):
    monkeypatch.setattr(
        "toolang.lang.format._format_source_lines",
        lambda *args, **kwargs: ["# Generated", "flow broken"],
    )
    with pytest.raises(ToolangFormatError) as caught:
        format_source("agic:\n  pass\n")
    assert "Formatter produced invalid syntax" in str(caught.value)
    assert "generated 2:1" in str(caught.value)
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
        assert "flow work(value: Text:" in rendered(caught.value, source)
    data = cst.to_data(cst.parse(source.encode()), source)
    diagnostic = data["diagnostics"][0]
    assert diagnostic["start_point"] == {"row": line - 1, "column": column - 1}
    assert diagnostic["message"] == "Expected ')' in parameter list"
    assert "flow work(value: Text:" not in diagnostic["message"]


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
    assert "{{#中文}}" in rendered(caught.value, source)
    assert "{{x中文}}" not in rendered(caught.value, source)


def test_error_selection_does_not_skip_an_earlier_generic_error():
    source = "@\nflow work(value: Text:\n  pass\n"
    with pytest.raises(ToolangSyntaxError) as caught:
        Program.from_source(source)
    assert caught.value.line == 1
    assert "'@'" in rendered(caught.value, source)
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
    assert "Text:" in rendered(caught.value, source)
    assert len(rendered(caught.value, source)) < 250


@pytest.mark.parametrize("missing", [True, False])
def test_unknown_grammar_node_has_safe_single_line_fallback(missing):
    source = "中文 \x1b[31m unknown\n".encode()
    node = cast(
        Node,
        SimpleNamespace(
            type="future_token" if missing else "invalid_future_construct",
            is_missing=missing,
            parent=None,
            children=[],
            is_error=not missing,
            start_byte=7,
            end_byte=len(source) - 1,
            start_point=Point(0, 7),
            end_point=Point(0, len(source) - 1),
        ),
    )
    diagnostic = syntax_diagnostic(node, source)
    assert diagnostic.reason == "Parse error"
    message = render_diagnostic(diagnostic, source=DiagnosticSource(source.decode()))
    assert "in source" not in message
    assert "unknown" in message
    assert "future_" not in message
    assert "\n" not in message and "\x1b" not in message


def test_cst_diagnostic_positions_do_not_repeatedly_scan_source_prefixes():
    class MeasuredSource(bytes):
        scanned_bytes = 0

        def count(self, sub, start=0, end=None):
            self.scanned_bytes += len(self[start:end])
            return super().count(sub, start, end)

    source = MeasuredSource(b"\xef\xbb\xbf" + b"flow broken:\r\n\trun\r\n" * 256)
    errors = cst.diagnostics(cst.parse(source).root_node, source)
    assert len(errors) == 256
    assert errors[-1]["start_point"] == {"row": 511, "column": 1}
    assert errors[-1]["message"] == "Malformed flow statement 'run'"
    # Allow a single indexing pass, but not one scan per diagnostic.
    assert source.scanned_bytes <= len(source)


@pytest.mark.parametrize(
    "source", [b"", "\ufeff中文".encode(), b"first\r\n\tlast", b"first\n"]
)
def test_source_position_clamps_synthetic_newline_to_original_eof(source):
    tree = cst.parse(source + b"\n@")
    generated = next(
        item
        for item in cst.diagnostics(tree.root_node, source + b"\n@")
        if item["start_byte"] > len(source)
    )
    node = tree.root_node.descendant_for_byte_range(
        generated["start_byte"], generated["end_byte"]
    )
    assert node is not None
    assert source_position(node, source) == (
        source.count(b"\n") + 1,
        len(source) - source.rfind(b"\n"),
    )


@pytest.mark.parametrize("error_type", [ToolangSyntaxError, ToolangFormatError])
def test_exception_payload_and_legacy_accessors_share_one_location(error_type):
    from dataclasses import FrozenInstanceError
    from toolang.lang.types import SourceDiagnostic, SourceLocation

    diagnostic = SourceDiagnostic("Expected a field type", SourceLocation(2, 9))
    error = error_type(diagnostic)
    assert error.diagnostic is diagnostic
    assert str(error) == "line 2:9: Expected a field type"
    assert isinstance(
        error, ValueError if error_type is ToolangFormatError else Exception
    )
    with pytest.raises(FrozenInstanceError):
        diagnostic.reason = "Changed"
    error.line = 3
    error.column = 5
    assert error.diagnostic.location == SourceLocation(3, 5)
    assert diagnostic.location == SourceLocation(2, 9)
    legacy = error_type("Expected a field type", line=2, column=9)
    assert legacy.diagnostic == diagnostic


def test_location_context_preserves_more_specific_location_and_original_cause():
    from toolang.lang.errors import ToolangValidationError, source_location
    from toolang.base.errors import ToolangError

    cause = ToolangError("Opaque message at line 12.\nMore context.")
    with pytest.raises(ToolangValidationError) as caught:
        with source_location(1, 1), source_location(3, 5):
            raise cause
    assert caught.value.__cause__ is cause
    assert caught.value.diagnostic.reason == str(cause)
    assert (caught.value.line, caught.value.column) == (3, 5)


def test_unknown_location_and_column_are_not_invented():
    from toolang.lang.types import SourceDiagnostic, SourceLocation

    error = ToolangFormatError("Could not format source")
    assert error.line is None and error.column is None
    assert (
        render_diagnostic(error.diagnostic, label="a.too")
        == "a.too: Could not format source"
    )
    assert (
        render_diagnostic(
            SourceDiagnostic("Invalid value", SourceLocation(3)), label="a.too"
        )
        == "a.too:3: Invalid value"
    )


@pytest.mark.parametrize(
    "source,reason,primary,previous",
    [
        (
            "skill review:\n  description = First.\n  description = Second.\n  Review.\n",
            "Duplicate property 'description' in skill 'review'",
            3,
            2,
        ),
        (
            "service remote:\n  description = Remote.\n  transport = http\n  protocol = stdio\n  target = server\n",
            "Properties 'transport' and 'protocol' in service 'remote' are mutually exclusive",
            4,
            3,
        ),
        (
            "agic work:\n  pass\nagic work:\n  pass\n",
            "Duplicate runnable name 'work'",
            3,
            1,
        ),
        (
            "context notes:\n  First.\ncontext notes:\n  Second.\n",
            "Duplicate context name 'notes'",
            3,
            1,
        ),
        (
            "struct Record:\n  value: Text\nstruct Record:\n  value: Text\n",
            "Duplicate struct name 'Record'",
            3,
            1,
        ),
    ],
)
def test_semantic_conflicts_keep_primary_and_related_locations(
    source, reason, primary, previous
):
    from toolang.lang.errors import ToolangValidationError

    with pytest.raises(ToolangValidationError) as caught:
        Program.from_source(source)
    diagnostic = caught.value.diagnostic
    assert diagnostic.reason == reason
    assert diagnostic.location is not None
    assert diagnostic.location.line == primary
    assert diagnostic.location.precision == "construct"
    assert diagnostic.related[0].location.line == previous
    output = render_diagnostic(
        diagnostic, label="conflict.too", source=DiagnosticSource(source)
    )
    assert f"conflict.too:{primary}:" in output
    assert f"conflict.too:{previous}: note:" in output
    assert "at line" not in output


@pytest.mark.parametrize(
    "kinds", [("flow", "agic"), ("agic", "flow"), ("flow", "flow", "agic")]
)
def test_duplicate_runnables_follow_source_order_across_declaration_kinds(kinds):
    from toolang.lang.errors import ToolangValidationError

    source = "".join(f"{kind} work:\n  pass\n" for kind in kinds)
    with pytest.raises(ToolangValidationError) as caught:
        Program.from_source(source)
    diagnostic = caught.value.diagnostic
    assert diagnostic.location is not None
    assert diagnostic.location.line == 3
    assert diagnostic.related[0].reason == "Previous declaration"
    assert diagnostic.related[0].location.line == 1


@pytest.mark.parametrize("space", [" ", "\u3000"], ids=["ascii", "unicode"])
@pytest.mark.parametrize("precision", ["recovery", "construct"])
def test_clipped_excerpt_marks_omitted_text_after_whitespace(space, precision):
    from toolang.lang.types import SourceLocation

    text = f"flow{space * 1000}?"
    source = DiagnosticSource(text)
    location = SourceLocation(1, 1, 1, len(source.source) + 1, precision=precision)
    assert source.excerpt(location) == "'flow…'"


def test_recovery_excerpt_is_bounded_escaped_and_keeps_multiline_context():
    from toolang.lang.types import SourceDiagnostic, SourceLocation

    source = "repeat:\n  \x1b[31m中文\n" + "x" * 10000
    diagnostic = SourceDiagnostic(
        "Parse error", SourceLocation(1, 1, 3, 10001, precision="recovery")
    )
    output = render_diagnostic(
        diagnostic, label="a.too", source=DiagnosticSource(source)
    )
    assert "\\n" in output and "\\x1b" in output and "中文" in output
    assert "\n" not in output and "\x1b" not in output
    assert "…" in output and len(output) < 180


@pytest.mark.parametrize(
    "kind,missing",
    [("ERROR", False), ("future_token", True), ("invalid_future_construct", False)],
)
def test_unknown_recovery_descendants_do_not_claim_greater_precision(kind, missing):
    from toolang.lang.diagnostics import primary_error

    root = SimpleNamespace(type="ERROR", is_error=True, is_missing=False, parent=None)
    child = SimpleNamespace(
        type=kind,
        is_error=kind == "ERROR",
        is_missing=missing,
        children=[],
        parent=root,
    )
    root.children = [child]
    assert primary_error(cast(Node, root)) is root


def test_program_wraps_unlocated_failures_without_inventing_a_location(monkeypatch):
    from toolang.base.errors import ToolangError
    from toolang.lang.errors import ToolangValidationError

    cause = ToolangError("Opaque failure at line 12.")

    def fail(program, *, external_flows=None):
        raise cause

    monkeypatch.setattr("toolang.lang.validate._validate", fail)
    with pytest.raises(ToolangValidationError) as caught:
        Program.from_source("agic work:\n  pass\n")
    assert caught.value.__cause__ is cause
    assert caught.value.diagnostic.location is None
    assert str(caught.value) == str(cause)


def test_recovery_without_an_end_point_keeps_the_source_line():
    from toolang.lang.types import SourceDiagnostic, SourceLocation

    diagnostic = SourceDiagnostic(
        "Parse error", SourceLocation(1, 5, precision="recovery")
    )
    assert (
        render_diagnostic(diagnostic, source=DiagnosticSource("abc broken\n"))
        == "line 1:5: Parse error: 'abc broken'"
    )


def test_many_diagnostics_on_one_long_line_do_not_redecode_the_line():
    from toolang.lang.types import SourceLocation

    class MeasuredBytes(bytes):
        decoded_bytes = 0

        def __getitem__(self, key):
            value = super().__getitem__(key)
            return type(self)(value) if isinstance(value, bytes) else value

        def decode(self, *args, **kwargs):
            type(self).decoded_bytes += len(self)
            return super().decode(*args, **kwargs)

    source = DiagnosticSource("x" * 100_000)
    source.lines[0] = MeasuredBytes(source.lines[0])
    for column in range(1, 100_000, 1000):
        assert source.excerpt(SourceLocation(1, column)) is not None
    # Allow indexing and bounded excerpts, but not a full line scan per error.
    assert MeasuredBytes.decoded_bytes <= 2 * len(source.source)
