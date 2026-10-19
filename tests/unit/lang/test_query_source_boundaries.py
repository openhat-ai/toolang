"""Query hash handling follows the grammar's directive boundaries."""

import pytest

from toolang.lang import Program, format_source
from toolang.lang.ast import _parse_tree


@pytest.mark.parametrize("marker", ["#@", "##", "## @param _"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix", ["", "\ufeff"])
def test_documentation_template_hashes_survive_formatting(marker, newline, prefix):
    text = "Résumé {{#flag}}yes{{/flag}} # literal."
    source = (f"{prefix}{marker} {text}\nagic work:\n  user: Work.\n").replace(
        "\n", newline
    )
    formatted = format_source(source)
    assert text in formatted
    assert format_source(formatted) == formatted
    for candidate in (source, formatted):
        program = Program.from_source(candidate)
        if marker == "#@":
            assert program.doc == text
        elif marker == "##":
            assert program.agics[0].doc == text
        else:
            assert program.agics[0].input is not None
            assert program.agics[0].input.doc == text


@pytest.mark.parametrize(
    "key", ["models", "tools", "skills", "services", "psyches", "prompts"]
)
@pytest.mark.parametrize("value,expected", [("tool#version", "tool"), ('"a#b"', '"a')])
def test_query_named_properties_keep_structural_comments(key, value, expected):
    source = f"chore work:\n  {key} = {value}\n  Work.\n"
    for candidate in (source, format_source(source)):
        assert Program.from_source(candidate).chores[0].meta[key] == expected


@pytest.mark.parametrize(
    "kind,entry", [("agic", "user:"), ("flow", "run:"), ("prompt", "")]
)
def test_query_like_text_remains_original_cst_bytes(kind, entry):
    source = (
        f"{kind} work:\n  {entry}\n    tools = vendor/tool#v1\n    {{{{#flag}}}}yes{{{{/flag}}}}\n"
        if entry
        else "prompt work:\n  Begin.\n  tools = vendor/tool#v1\n  {{#flag}}yes{{/flag}}\n"
    )
    encoded = source.encode()
    tree = _parse_tree(encoded)
    pending = [tree.root_node]
    while pending:
        node = pending.pop()
        assert node.text == encoded[node.start_byte : node.end_byte]
        pending.extend(node.named_children)


@pytest.mark.parametrize(
    "kind,entry", [("agic", "user: Work."), ("flow", "run: Work.")]
)
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_real_query_hashes_keep_original_values_and_source_positions(
    kind, entry, newline
):
    query = 'vendor/tool#v1[description="Résumé # exact"]'
    source = f"{kind} work:\n  tools = {query} # Keep {{#note}}.\n  {entry}\n".replace(
        "\n", newline
    )
    for candidate in (source, format_source(source)):
        program = Program.from_source(candidate)
        owner = program.agics[0] if kind == "agic" else program.flows[0]
        assert owner.directives[0].values == (query,)
        assert owner.directives[0].span.line == 2


@pytest.mark.parametrize(
    "key,value",
    [
        ("context", "none"),
        ("instruct", "none"),
        ("hands", "none"),
        ("handoffs", "none"),
        ("recall", "near"),
        ("lanes", "2"),
    ],
)
def test_non_query_directive_comments_preserve_punctuation(key, value):
    comment = "# Keep,exact,comma,"
    source = f"agic work:\n  {key}={value}{comment}\n  user: Work.\n"
    formatted = format_source(source)
    assert f"{key} = {value}  {comment}" in formatted
    assert format_source(formatted) == formatted
    Program.from_source(formatted)
