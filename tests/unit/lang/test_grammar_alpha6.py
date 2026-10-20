"""Alpha.6 syntax and recovery survive lowering, formatting, and inspection."""

import pytest
from tree_sitter import Node, Parser

from toolang.lang import Program, cst, format_source, to_data
from toolang.lang.ast import LetStmt, ReduceStmt, flow_stmt_from_data
from toolang.lang.errors import ToolangFormatError, ToolangSyntaxError
from toolang.lang.highlight import captures


def _field(node: Node, name: str) -> Node:
    child = node.child_by_field_name(name)
    assert child is not None, (node, name)
    return child


@pytest.mark.parametrize("target", ["named", "inline"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("ending", ["", "\n"])
@pytest.mark.parametrize("multiline", [False, True])
def test_initial_content_reaches_ast_and_survives_formatting(
    target, newline, ending, multiline
):
    literal = 'Seed https://example.com/#details "a#b" 01 times'
    header = (
        "reduce using combine:\n"
        if target == "named"
        else "reduce:\n    Merge {{_}} with {{_1._}}.\n"
    )
    initial = (
        f"    initial:\n\n      {literal}" if multiline else f"    initial: {literal}"
    )
    source = (
        "agic combine(_: Text) -> Text:\n  user: {{_}} {{_1._}}\n"
        "flow work(_: Text[]) -> Text:\n  " + header + initial + ending
    ).replace("\n", newline)
    root = cst.parse(source.encode()).root_node
    assert not list(cst.error_nodes(root))
    body = _field(root.named_children[-1], "body")
    reducer = _field(body, "stmt")
    assert reducer.type == "reduce_stmt"
    content = _field(reducer, "initial")
    assert content.type == "content"
    assert [child.type for child in content.named_children if child.type == "text"] == [
        "text"
    ]
    for candidate in (source, format_source(source)):
        statement = Program.from_source(candidate).flows[0].stmts[0]
        assert isinstance(statement, ReduceStmt)
        assert statement.initial == literal
        assert flow_stmt_from_data(to_data(statement)) == statement
    formatted = format_source(source)
    assert literal in formatted and format_source(formatted) == formatted


def test_former_initializer_word_is_ordinary_text_and_a_contextual_name():
    source = (
        "flow work(_: Text[]):\n"
        "  let from = Literal # text\n"
        "  reduce:\n"
        "    Merge {{_}}.\n"
        "    from: Seed # ordinary text\n"
    )
    for candidate in (source, format_source(source)):
        program = Program.from_source(candidate)
        binding, reducer = program.flows[0].stmts
        assert isinstance(binding, LetStmt) and binding.binding == "from"
        assert isinstance(reducer, ReduceStmt) and reducer.initial is None
        target = program.find_agic(reducer.runnable)
        assert target is not None
        assert target.messages[0].content == "Merge {{_}}.\nfrom: Seed # ordinary text"


@pytest.mark.parametrize("text", ["from: Seed.", "unrecognized: Seed."])
def test_named_initializer_scope_rejects_ordinary_lines_without_special_compatibility(
    text,
):
    source = (
        f"flow work(_: Text[]):\n  reduce using combine:\n    {text}\n"
        "## Intact.\nagic combine(_: Text):\n  user: {{_}}\n"
    )
    root = cst.parse(source.encode()).root_node
    errors = list(cst.error_nodes(root))
    assert errors and all(
        node.end_byte <= source.index("## Intact.") for node in errors
    )
    assert _field(root.named_children[-1], "name").text == b"combine"
    for parse, error in [
        (Program.from_source, ToolangSyntaxError),
        (format_source, ToolangFormatError),
    ]:
        with pytest.raises(error):
            parse(source)


def test_deeper_initial_text_and_initially_prose_are_literal():
    source = (
        "flow work(_: Text[]):\n"
        "  reduce:\n"
        "    Initially merge {{_}}.\n"
        "      initial: Literal # text\n"
        "    initial: Seed.\n"
    )
    for candidate in (source, format_source(source)):
        program = Program.from_source(candidate)
        reducer = program.flows[0].stmts[0]
        assert isinstance(reducer, ReduceStmt) and reducer.initial == "Seed."
        target = program.find_agic(reducer.runnable)
        assert target is not None
        assert target.messages[0].content == (
            "Initially merge {{_}}.\n  initial: Literal # text"
        )


@pytest.mark.parametrize("modifier", ["", "async "])
@pytest.mark.parametrize("binding", ["", "let result = "])
@pytest.mark.parametrize("tail", [" extra", "()", ".field", "[0]"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_native_run_recovery_is_local_and_incremental(modifier, binding, tail, newline):
    source = (
        (
            f"flow work:\n  {binding}{modifier}run worker\n"
            "  ## After.\n  run after\n## Next.\nagic next:\n  user: Intact.\n"
        )
        .replace("\n", newline)
        .encode()
    )
    parser = Parser(cst.language())
    tree = parser.parse(source)
    point = source.index(b"worker") + len(b"worker")
    start_point = (1, point - source.index(newline.encode()) - len(newline))
    broken = source[:point] + tail.encode() + source[point:]
    tree.edit(
        start_byte=point,
        old_end_byte=point,
        new_end_byte=point + len(tail),
        start_point=start_point,
        old_end_point=start_point,
        new_end_point=(start_point[0], start_point[1] + len(tail)),
    )
    incremental = parser.parse(broken, tree)
    for parsed in (incremental, cst.parse(broken)):
        root = parsed.root_node
        flow = root.named_children[0]
        body = _field(flow, "body")
        run, after = body.children_by_field_name("stmt")
        if binding:
            run = _field(run, "stmt")
        assert run.type == "run_stmt"
        assert _field(run, "target").text == b"worker"
        assert _field(after, "target").text == b"after"
        errors = list(cst.error_nodes(root))
        assert len(errors) == 1 and errors[0].is_error
        assert errors[0].text == tail.strip().encode()
        assert errors[0].end_byte < after.start_byte
        assert body.named_children[1].type == "item_doc"
        assert root.named_children[-2].type == "item_doc"
        assert _field(root.named_children[-1], "name").text == b"next"
    assert cst.to_data(incremental, broken.decode()) == cst.to_data(
        cst.parse(broken), broken.decode()
    )
    assert any(
        capture.name == "error"
        and broken[capture.start_byte : capture.end_byte] == tail.strip().encode()
        for capture in captures(broken)
    )
    for parse, error in [
        (Program.from_source, ToolangSyntaxError),
        (format_source, ToolangFormatError),
    ]:
        with pytest.raises(error):
            parse(broken.decode())


@pytest.mark.parametrize(
    "key,node_type", [("models", "query_value"), ("context", "identifier")]
)
def test_native_missing_values_are_visible_and_rejected(key, node_type):
    source = f"flow work:\n  {key} =\n  run worker\nagic next:\n  user: Intact.\n"
    root = cst.parse(source.encode()).root_node
    directive = _field(_field(root.named_children[0], "body"), "directive")
    value = _field(directive, "value")
    assert value.type == node_type and value.is_missing and not value.children
    diagnostic = next(
        item
        for item in cst.diagnostics(root, source.encode())
        if item["kind"] == "missing"
    )
    assert diagnostic["node_type"] == node_type
    assert diagnostic["start_byte"] == diagnostic["end_byte"]
    for parse, error in [
        (Program.from_source, ToolangSyntaxError),
        (format_source, ToolangFormatError),
    ]:
        with pytest.raises(error):
            parse(source)
