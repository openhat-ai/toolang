from __future__ import annotations

import pytest

from toolang.lang import Program
from toolang.lang.ast import RepeatStmt


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
  repeat 2:
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
