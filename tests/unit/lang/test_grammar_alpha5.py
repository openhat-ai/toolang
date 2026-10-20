"""Compatibility with the published alpha.5 grammar and its concrete tree."""

from importlib.metadata import version

import pytest

from toolang.lang import Program, format_source
from toolang.lang import cst
from toolang.lang.ast import LetStmt, RepeatStmt
from toolang.lang.errors import (
    ToolangFormatError,
    ToolangSyntaxError,
    ToolangValidationError,
)
from toolang.lang.highlight import captures, resolve


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("eof", ["", "\n"])
@pytest.mark.parametrize(
    "text",
    [
        "Visit https://example.com/#details",
        '"a#b" # still text',
        "# First line",
        "##! Literal heading",
    ],
)
@pytest.mark.parametrize(
    "entry", ["let", "run", "message", "context", "prompt", "from"]
)
def test_explicit_text_preserves_hashes_and_ordinary_quotes(newline, eof, text, entry):
    sources = {
        "let": f"flow work():\n  let note = {text}",
        "run": f"flow work():\n  run: {text}",
        "message": f"agic work():\n  user: {text}",
        "context": f"context notes: {text}",
        "prompt": f"prompt notes:\n  Begin.\n  {text}",
        "from": f"flow work():\n  run -> Text[]: Items.\n  reduce:\n    Merge {{{{_}}}}.\n    from: {text}",
    }
    source = (sources[entry] + eof).replace("\n", newline)
    formatted = format_source(source)
    assert text in formatted
    assert format_source(formatted) == formatted
    for candidate in (source, formatted):
        program = Program.from_source(candidate)
        if entry == "let":
            statement = program.flows[0].stmts[0]
            assert isinstance(statement, LetStmt) and statement.value == text
        elif entry in {"run", "message"}:
            assert program.agics[0].messages[0].content == text
        elif entry == "context":
            assert program.contexts[0].body == text
        elif entry == "prompt":
            assert program.caps[0].body == f"Begin.\n{text}"
        else:
            assert getattr(program.flows[0].stmts[-1], "initial") == text


@pytest.mark.parametrize("count", ["0", "1", "01", "2", "002"])
@pytest.mark.parametrize("unit", ["time", "times"])
def test_repeat_units_are_canonicalized_by_value(count, unit):
    source = f"flow work():\n  repeat {count} {unit}:\n    run: Work.\n"
    formatted = format_source(source)
    expected = int(count)
    assert f"repeat {expected} {'time' if expected == 1 else 'times'}:" in formatted
    for candidate in (source, formatted):
        statement = Program.from_source(candidate).flows[0].stmts[0]
        assert isinstance(statement, RepeatStmt) and statement.count == expected
    assert format_source(formatted) == formatted


@pytest.mark.parametrize("count", ["1", "01", "2", "002"])
@pytest.mark.parametrize("unit", ["lane", "lanes"])
@pytest.mark.parametrize(
    "operation", ["generate 2", "map", "keep", "drop", "sort ascending"]
)
def test_lane_units_and_position_share_one_contract(count, unit, operation):
    connector = (
        "by"
        if operation.startswith("sort")
        else "if"
        if operation in {"keep", "drop"}
        else ""
    )
    target = (
        " -> Number"
        if connector == "by"
        else " -> Boolean"
        if connector == "if"
        else ""
    )
    source = f"flow work():\n  run -> Text[]: Items.\n  {operation} in {count} {unit} {connector}{target}: Work with {{{{_}}}}.\n"
    formatted = format_source(source)
    assert f"in {int(count)} {'lane' if int(count) == 1 else 'lanes'}" in formatted
    for candidate in (source, formatted):
        assert getattr(
            Program.from_source(candidate).flows[0].stmts[-1], "lanes"
        ) == int(count)
    assert format_source(formatted) == formatted


@pytest.mark.parametrize(
    "name", ["sort", "using", "let", "run", "repeat", "until", "tools", "default"]
)
def test_contextual_names_do_not_change_text_or_structure(name):
    source = f"flow work({name}: Text):\n  let {name} = Keep # literal.\n  run: {{{{{name}}}}}\n"
    for candidate in (source, format_source(source)):
        flow = Program.from_source(candidate).flows[0]
        assert flow.params[0].name == name
        statement = flow.stmts[0]
        assert isinstance(statement, LetStmt)
        assert statement.binding == name and statement.value == "Keep # literal."


@pytest.mark.parametrize(
    "word",
    [
        "scatter",
        "gather",
        "storm",
        "settle",
        "rank",
        "par",
        "top",
        "bottom",
        "think",
        "use",
        "thunk",
        "call",
        "do",
        "unfold",
        "each",
        "fold",
        "head",
        "tail",
    ],
)
@pytest.mark.parametrize("position", ["statement", "continuation", "binding"])
def test_inactive_words_remain_prose_and_contextual_names(word, position):
    prose = f"{word} is ordinary prose."
    body = {
        "statement": f"  {prose}\n",
        "continuation": f"  First line.\n  {prose}\n",
        "binding": f"  let {word} = {prose}\n",
    }[position]
    source = f"flow work({word}: Text):\n{body}"
    for candidate in (source, format_source(source)):
        program = Program.from_source(candidate)
        flow = program.flows[0]
        assert flow.params[0].name == word
        assert len(flow.stmts) == 1
        statement = flow.stmts[0]
        if position == "binding":
            assert isinstance(statement, LetStmt)
            assert statement.binding == word and statement.value == prose
        else:
            target = program.find_agic(getattr(statement, "runnable"))
            assert target is not None
            assert target.messages[0].content == (
                f"First line.\n{prose}" if position == "continuation" else prose
            )


def test_program_documentation_and_first_member_ownership():
    source = "#@ Program.\n## Work.\n## @param sort Input.\nflow work(sort: Text):\n  ## First.\n  run: {{sort}}\n  ## Second.\n  run: Done.\n"
    tree = cst.parse(source.encode())
    assert version("tree-sitter-toolang") == "0.4.0a5"
    assert tree.root_node.type == "program"
    assert [child.type for child in tree.root_node.named_children[:3]] == [
        "program_doc",
        "item_doc",
        "param_doc",
    ]
    parameter = tree.root_node.named_children[2]
    name = parameter.child_by_field_name("param")
    description = parameter.child_by_field_name("description")
    assert name is not None and name.type == "param_name"
    assert description is not None and description.type == "text"
    declaration = tree.root_node.named_children[3]
    body = declaration.child_by_field_name("body")
    assert body is not None and body.start_point.row == 4
    assert [
        child.type for child in body.named_children if child.type == "item_doc"
    ] == ["item_doc", "item_doc"]
    keyword = declaration.child_by_field_name("keyword")
    assert keyword is not None and keyword.is_named is False
    for candidate in (source, format_source(source)):
        program = Program.from_source(candidate)
        assert program.doc == "Program."
        assert program.flows[0].doc == "Work."
        assert program.flows[0].params[0].doc == "Input."
        assert [item.doc for item in program.flows[0].stmts] == ["First.", "Second."]


@pytest.mark.parametrize("kind", ["prompt", "psyche", "skill", "service", "chore"])
def test_property_prefix_is_open_schema_and_one_way(kind):
    source = f"{kind} example:\n  answer = yes\n\n  # Prefix comment.\n  extra = data\n  Body.\n  answer = literal\n"
    root = cst.parse(source.encode()).root_node
    assert list(cst.error_nodes(root)) == []
    body = root.named_children[0].child_by_field_name("body")
    assert body is not None
    assert [
        key.text
        for child in body.children_by_field_name("property")
        if (key := child.child_by_field_name("key")) is not None
    ] == [b"answer", b"extra"]
    content = body.child_by_field_name("content")
    assert (
        content is not None
        and content.text is not None
        and b"answer = literal" in content.text
    )
    assert format_source(format_source(source)) == format_source(source)
    if kind != "chore":
        with pytest.raises(ToolangValidationError):
            Program.from_source(source)


@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_unknown_directive_recovery_preserves_following_declaration(kind):
    source = f"{kind} broken:\n  unknown = yes\nagic intact:\n  user: Keep # text.\n"
    root = cst.parse(source.encode()).root_node
    assert list(cst.error_nodes(root))
    assert any(
        (name := child.child_by_field_name("name")) is not None
        and name.text == b"intact"
        for child in root.named_children
    )
    for parse, error in [
        (Program.from_source, ToolangSyntaxError),
        (format_source, ToolangFormatError),
    ]:
        with pytest.raises(error):
            parse(source)


def test_anonymous_terminals_and_narrow_chore_highlighting():
    source = (
        b"chore deploy:\n  Keep # text.\nagic recall:\n  recall = near\n  user: Work.\n"
    )
    found = resolve(captures(source))
    assert any(
        item.name == "keyword" and source[item.start_byte : item.end_byte] == b"chore"
        for item in found
    )
    assert any(
        item.name == "property" and source[item.start_byte : item.end_byte] == b"recall"
        for item in found
    )
    assert not any(
        item.name.startswith("comment")
        and b"# text" in source[item.start_byte : item.end_byte]
        for item in found
    )


def test_formatter_handles_large_numeric_spelling_without_semantic_conversion():
    digits = "9" * 5000
    source = f"flow work():\n  repeat {digits} time:\n    run: Done.\n"
    formatted = format_source(source)
    assert f"repeat {digits} times:" in formatted
    with pytest.raises(ToolangValidationError, match="oversized integer"):
        Program.from_source(source)


@pytest.mark.parametrize(
    "source,diagnostic",
    [
        ("flow broken(x: ):\n  pass\n", "invalid_param"),
        ("agic broken:\n  recall += near\n  user: Work.\n", "invalid_directive"),
        ("flow broken:\n  async run worker extra\n", "invalid_run_target"),
        ("flow broken:\n  run\n", "invalid_flow_statement"),
        ("agic broken:\n  user, broken\n", "invalid_agic_message"),
        ("flow broken:\n", "invalid_missing_body"),
        ("flow broken:\n  run:\n", "invalid_missing_content"),
        ("flow broken:\n  repeat:\n    until: Done.\n", "invalid_missing_statement"),
        ("flow broken -> :\n  pass\n", "invalid_missing_type"),
    ],
)
def test_all_public_invalid_nodes_reject_source_without_losing_next_item(
    source, diagnostic
):
    source += "agic intact:\n  user: Keep.\n"
    root = cst.parse(source.encode()).root_node
    assert diagnostic in {node.type for node in cst.error_nodes(root)}
    assert any(
        (name := child.child_by_field_name("name")) is not None
        and name.text == b"intact"
        for child in root.named_children
    )
    for parse, error in [
        (Program.from_source, ToolangSyntaxError),
        (format_source, ToolangFormatError),
    ]:
        with pytest.raises(error):
            parse(source)


@pytest.mark.parametrize("kind", ["skill", "service", "prompt", "psyche", "chore"])
def test_empty_definition_does_not_own_next_declaration_documentation(kind):
    source = f"{kind} empty:\n## Next.\nagic next:\n  user: Work.\n"
    root = cst.parse(source.encode()).root_node
    assert list(cst.error_nodes(root)) == []
    definition, document, following = [
        child for child in root.named_children if child.type != "newline"
    ]
    assert definition.child_by_field_name("body") is None
    assert document.type == "item_doc" and document.parent == root
    assert source.encode()[definition.end_byte : following.start_byte].startswith(
        b"## Next."
    )


@pytest.mark.parametrize(
    "operation",
    [
        "run",
        "exec",
        "spawn",
        "seek reviewer",
        "generate 2 using",
        "map using",
        "reduce using",
        "keep if",
        "drop if",
        "sort ascending by",
    ],
)
def test_named_call_targets_are_direct_role_leaves(operation):
    source = f"flow work:\n  {operation} worker\n"
    root = cst.parse(source.encode()).root_node
    assert list(cst.error_nodes(root)) == []
    body = root.named_children[0].child_by_field_name("body")
    assert body is not None
    statement = body.children_by_field_name("statement")[0]
    target = statement.child_by_field_name("target")
    assert (
        target is not None
        and target.type == "runnable_name"
        and target.text == b"worker"
    )


@pytest.mark.parametrize(
    "kind,member", [("agic", "user: Work."), ("flow", "run: Work.")]
)
def test_directive_prefix_allows_trivia_and_cannot_resume(kind, member):
    source = (
        f"{kind} work:\n  tools = none\n\n  # Prefix.\n  recall = near\n  {member}\n"
    )
    for candidate in (source, format_source(source)):
        program = Program.from_source(candidate)
        owner = program.agics[0] if kind == "agic" else program.flows[0]
        assert [directive.name for directive in owner.directives] == ["tools", "recall"]
    late = source + "  tools = none\n"
    for parse, error in [
        (Program.from_source, ToolangSyntaxError),
        (format_source, ToolangFormatError),
    ]:
        with pytest.raises(error):
            parse(late)
