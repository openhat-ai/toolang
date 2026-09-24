"""Raw source inspection retains the complete concrete tree."""

import pytest

from toolang.lang import cst
from toolang.lang.ast import Program
from toolang.lang.errors import ToolangValidationError
from toolang.cli.toolang.source_output import cst_sexp


@pytest.mark.parametrize(
    "source",
    [
        "",
        "agic echo:\n  Hello.\n",
        "# 🧭 中文\r\nagic echo(_):\r\n\t{{_}}  ",
        "## @param _ Input.\nagic echo(_):\n  {{_}}\n",
        "flow broken:\n  run\n",
        "struct Data:\n  field: Text\n",
        'agic echo:\n  tools = fs/*[description="hash#data"]\n  hi\n',
    ],
)
def test_complete_tree_matches_parser_and_original_bytes(source):
    encoded = source.encode()
    tree = cst.parse(encoded)
    data = cst.to_data(tree, source)
    assert data["source"].encode() == encoded
    assert data["schema_version"] == 1
    assert data["grammar"]["name"] == "toolang"
    assert data["grammar"]["version"].startswith("0.3.")
    pending = [(tree.root_node, data["root"], None)]
    while pending:
        node, projected, field = pending.pop()
        assert projected["field"] == field
        assert projected["type"] == node.type
        for flag in ("is_named", "is_extra", "is_error", "is_missing", "has_error"):
            assert projected[flag] == getattr(node, flag)
        assert encoded[projected["start_byte"] : projected["end_byte"]] == node.text
        for side in ("start", "end"):
            point = getattr(node, f"{side}_point")
            assert projected[f"{side}_point"] == {
                "row": point.row,
                "column": point.column,
            }
        assert len(projected["children"]) == len(node.children)
        pending.extend(
            (child, projected["children"][index], node.field_name_for_child(index))
            for index, child in enumerate(node.children)
        )
    assert " ".join(cst_sexp(tree.root_node).split()) == str(tree.root_node)


def test_invalid_grammar_nodes_are_diagnosed_without_native_error_flag():
    tree = cst.parse(b"flow broken:\n  run\n")
    assert not tree.root_node.has_error
    errors = cst.diagnostics(tree.root_node)
    assert errors[0]["kind"] == "invalid"
    assert errors[0]["node_type"] == "invalid_flow_reserved_statement"


def test_raw_parse_does_not_append_final_newline():
    source = "agic echo:\n  Hello."
    tree = cst.parse(source.encode())
    assert tree.root_node.end_byte == len(source.encode())
    assert cst.to_data(tree, source)["source"] == source
    assert Program.from_source(source).agics[0].messages[0].content == "Hello."


def test_cst_syntax_does_not_validate_parameter_documentation():
    source = "## @param unknown Details.\nagic echo(_):\n  {{_}}\n"
    assert cst.diagnostics(cst.parse(source.encode()).root_node) == []
    with pytest.raises(ToolangValidationError, match="unknown"):
        Program.from_source(source)


def test_missing_nodes_keep_native_markers_and_zero_width_ranges():
    source = "struct X:\n  field:\n"
    tree = cst.parse(source.encode())
    errors = cst.diagnostics(tree.root_node)
    missing = next(item for item in errors if item["kind"] == "missing")
    assert missing["start_byte"] == missing["end_byte"]
    assert '(MISSING "Text")' in cst_sexp(tree.root_node)
    assert " ".join(cst_sexp(tree.root_node).split()) == str(tree.root_node)
