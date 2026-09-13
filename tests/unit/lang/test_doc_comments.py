from __future__ import annotations

import pytest

from toolang.lang import Program
from toolang.lang.ast import RepeatStmt, program_from_data, to_data
from toolang.lang.errors import ToolangSyntaxError, ToolangValidationError


def test_program_docs_are_collected_from_top_level_comments() -> None:
    program = Program.from_source(
        """
##! Toolang program.
agic first:
  First body.

##! Documents all declarations.
##! Not only the following one.
agic second:
  Second body.
"""
    )

    assert program.doc == (
        "Toolang program.\nDocuments all declarations.\nNot only the following one."
    )
    assert all(agic.doc is None for agic in program.agics)
    assert [message.content for message in program.agics[0].messages] == ["First body."]


def test_declaration_docs_attach_after_a_previous_body() -> None:
    program = Program.from_source(
        """
agic first:
  First body.

## Search the web.
## Return source-backed evidence.
agic search:
  Search body.

## Run the complete research pipeline.
flow research:
  run search
"""
    )

    first = program.find_agic("first")
    search = program.find_agic("search")
    research = program.find_flow("research")
    assert first is not None
    assert search is not None
    assert research is not None
    assert first.doc is None
    assert [message.content for message in first.messages] == ["First body."]
    assert search.doc == "Search the web.\nReturn source-backed evidence."
    assert research.doc == "Run the complete research pipeline."


def test_docs_attach_to_nodes_in_nested_scopes() -> None:
    program = Program.from_source(
        """
struct Finding:
  ## Human-readable title.
  title: Text

agic worker:
  ## Model request body.
  Find relevant evidence.

flow research:
  ## Repeat searches.
  repeat 2 times:
    ## Run one search.
    run worker
"""
    )

    assert program.structs[0].fields[0].doc == "Human-readable title."
    worker = program.find_agic("worker")
    assert worker is not None
    assert worker.messages[0].doc == "Model request body."
    repeat = program.flows[0].stmts[0]
    assert isinstance(repeat, RepeatStmt)
    assert repeat.doc == "Repeat searches."
    assert repeat.stmts[0].doc == "Run one search."
    assert [agic.name for agic in program.agics] == ["worker"]


@pytest.mark.parametrize(
    "separator",
    [
        "\n",
        "# Ordinary comment.\n",
    ],
)
def test_blank_lines_and_ordinary_comments_end_doc_attachment(
    separator: str,
) -> None:
    program = Program.from_source(
        f"""
## Detached documentation.
{separator}agic worker:
  Work.
"""
    )

    assert program.agics[0].doc is None


def test_other_syntax_items_end_doc_attachment() -> None:
    program = Program.from_source(
        """
agic worker:
  ## This documents neither the directive nor the message.
  tools += shell
  Work.
"""
    )

    agic = program.agics[0]
    assert agic.directives[0].doc is None
    assert agic.messages[0].doc is None


def test_indented_program_docs_are_not_program_docs() -> None:
    program = Program.from_source(
        """
agic worker:
  ##! Not program documentation.
  Work.
"""
    )

    assert program.doc is None
    assert program.agics[0].messages[0].doc is None


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("ending", ["", "\n"])
def test_module_comments_use_structured_text_and_keep_source_order(
    newline: str, ending: str
) -> None:
    source = (
        "#!/usr/bin/env too\n#@ Résumé.  \n#@\n##! Legacy overview.\n"
        "## Detached item text.\n#@ @param _ Module prose.\n"
        "agic worker:\n  pass\n#@ Final module line." + ending
    ).replace("\n", newline)
    program = Program.from_source(source)
    assert program.doc == (
        "Résumé.\nLegacy overview.\n@param _ Module prose.\nFinal module line."
    )
    assert program.agics[0].doc is None
    assert program.agics[0].span.line == 7


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("name", [" work", ""])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_parameter_docs_bind_by_name_without_changing_signatures(
    kind: str, name: str, newline: str
) -> None:
    program = Program.from_source(
        (
            "## Summarize material.\n"
            "##@param\tstyle\tRésumé # @param literal.  \n"
            "##\n"
            "## @param _ Source material.\n"
            "## Return a short overview.\n"
            f"{kind}{name}(_: Text, style?: Text, count: Number) -> Text:\n"
            "  pass\n"
        ).replace("\n", newline)
    )
    runnable = (*program.agics, *program.flows)[0]
    assert runnable.doc == "Summarize material.\nReturn a short overview."
    assert runnable.input is not None
    assert (runnable.input.doc, runnable.input.type_name, runnable.input.optional) == (
        "Source material.",
        "Text",
        False,
    )
    assert [
        (param.name, param.doc, param.type_name, param.optional)
        for param in runnable.params
    ] == [
        ("style", "Résumé # @param literal.", "Text", True),
        ("count", None, "Number", False),
    ]
    assert runnable.output == "Text"
    assert program_from_data(to_data(program)) == program


@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_parameter_docs_support_implicit_primary_input(kind: str) -> None:
    program = Program.from_source(f"## @param _ Request.\n{kind}:\n  pass\n")
    runnable = (*program.agics, *program.flows)[0]
    assert runnable.doc is None
    assert runnable.input is not None
    assert runnable.input.doc == "Request."
    assert runnable.input.type_name == "Part[]"


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize(
    "signature, name",
    [("()", "_"), ("(topic: Text)", "_"), ("", "topic"), ("(topic: Text)", "other")],
)
def test_unknown_parameter_documentation_is_rejected(
    kind: str, signature: str, name: str
) -> None:
    with pytest.raises(
        ToolangValidationError, match=f"Unknown parameter {name!r}.*line 1"
    ):
        Program.from_source(
            f"## @param {name} Description.\n{kind}{signature}:\n  pass\n"
        )


def test_duplicate_parameter_documentation_is_rejected() -> None:
    with pytest.raises(
        ToolangValidationError, match="Duplicate documentation.*'_' .*line 3"
    ):
        Program.from_source(
            "## @param _ First.\n## Description.\n## @param _ Second.\nagic:\n  pass\n"
        )


@pytest.mark.parametrize(
    "source",
    [
        "## @param _ Field.\nstruct Item:\n  value: Text\n",
        "struct Item:\n  ## @param _ Field.\n  value: Text\n",
        "## @param topic Template.\nprompt review:\n  Review {{topic}}.\n",
        "agic:\n  ## @param _ Directive.\n  tools = shell\n  Work.\n",
        "agic:\n  ## @param _ Message.\n  Work.\n",
        "flow:\n  ## @param _ Statement.\n  run: Work.\n",
        "flow:\n  ## @param _ Repeat.\n  repeat 1 time:\n    run: Work.\n",
    ],
)
def test_parameter_docs_reject_non_runnable_targets(source: str) -> None:
    with pytest.raises(ToolangValidationError, match="must attach to an agic or flow"):
        Program.from_source(source)


@pytest.mark.parametrize(
    "separator", ["\n", "# Plain.\n", "#@ Module.\n", "##! Module.\n"]
)
def test_detached_parameter_tags_do_not_bind_or_validate(separator: str) -> None:
    program = Program.from_source(
        "## @param unknown Detached.\n## @param unknown Duplicate but detached.\n"
        + separator
        + "agic:\n  pass\n## @param unknown End of file."
    )
    assert program.agics[0].doc is None
    assert program.agics[0].input is not None
    assert program.agics[0].input.doc is None


def test_parameter_tags_at_scope_end_do_not_attach_after_dedent() -> None:
    program = Program.from_source(
        "flow first:\n  pass\n  ## @param unknown Detached.\nagic next:\n  pass\n"
    )
    assert program.agics[0].doc is None


@pytest.mark.parametrize(
    "tag", ["@param", "@param _", "@param _   ", "@param bad-name Description."]
)
def test_malformed_parameter_tags_remain_syntax_errors(tag: str) -> None:
    with pytest.raises(ToolangSyntaxError, match="line 1"):
        Program.from_source(f"## {tag}\nagic:\n  pass\n")


@pytest.mark.parametrize(
    "text",
    [
        "@parameter _ Text.",
        "@parametric",
        "@param: text",
        "@return Text.",
        "Use @param here.",
    ],
)
def test_other_documentation_tags_remain_prose(text: str) -> None:
    program = Program.from_source(f"## {text}\nagic:\n  pass\n")
    assert program.agics[0].doc == text


@pytest.mark.parametrize("marker", ["#", "#!", "##", "#@", "##!", "## @param"])
def test_first_explicit_text_line_is_never_documentation(marker: str) -> None:
    body = f"{marker} Literal text.\n{marker} More text."
    source = "agic:\n  user:\n" + "\n".join(f"    {line}" for line in body.splitlines())
    program = Program.from_source(source)
    assert program.doc is None
    assert program.agics[0].doc is None
    assert program.agics[0].messages[0].doc is None
    assert program.agics[0].messages[0].content == body


@pytest.mark.parametrize(
    "comment, module_doc, runnable_doc, input_doc",
    [
        ("#@ Module.", "Module.", None, None),
        ("##! Module.", "Module.", None, None),
        ("## Runnable.", None, "Runnable.", None),
        ("## @param _ Input.", None, None, "Input."),
    ],
)
def test_leading_bom_preserves_first_documentation_comment(
    comment, module_doc, runnable_doc, input_doc
):
    program = Program.from_source(f"\ufeff{comment}\nagic:\n  pass\n")
    assert program.doc == module_doc
    assert program.agics[0].doc == runnable_doc
    assert program.agics[0].input is not None
    assert program.agics[0].input.doc == input_doc


@pytest.mark.parametrize(
    "comments, error",
    [
        ("## @param unknown Input.\n", "Unknown parameter"),
        ("## @param _ First.\n## @param _ Second.\n", "Duplicate documentation"),
    ],
)
def test_leading_bom_does_not_bypass_parameter_validation(comments, error):
    with pytest.raises(ToolangValidationError, match=error):
        Program.from_source(f"\ufeff{comments}agic:\n  pass\n")
