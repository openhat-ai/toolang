from __future__ import annotations

import pytest

from toolang.base.error import ToolangError
from toolang.lang.ast import (
    AskStmt,
    DropStmt,
    KeepStmt,
    LetStmt,
    MapStmt,
    Program,
    RankStmt,
    RepeatStmt,
    RunStmt,
    ScatterStmt,
    SeekStmt,
    StormStmt,
)


FLOW_SOURCE = """
agic helper(in: Text) -> Text:
  user: {{in}}

flow pipeline(in: Text) -> Text:
  run helper
  run -> Text: inline run
  bare inline run
  seek alice helper
  seek bob -> Text: inline seek
  ask: continue?
  scatter 3 helper
  storm 4 helper par 2
  gather helper
  settle helper
  map helper par 2
  keep first 2
  keep helper par 2
  drop last 1
  rank helper top 3 par 2
  repeat 2:
    run helper
    until: done?
  let result = run helper
  let run helper
  let note: authored text
"""


def test_flow_statements_lower_to_specific_nodes() -> None:
    program = Program.from_source(FLOW_SOURCE)
    flow = program.flows[0]

    assert flow.name == "pipeline"
    assert flow.input is not None and flow.input.type_name == "Text"
    assert flow.output == "Text"
    assert [stmt.kind for stmt in flow.stmts] == [
        "run",
        "run",
        "run",
        "seek",
        "seek",
        "ask",
        "scatter",
        "storm",
        "gather",
        "settle",
        "map",
        "keep",
        "keep",
        "drop",
        "rank",
        "repeat",
        "run",
        "run",
        "let",
    ]

    assert isinstance(flow.stmts[0], RunStmt)
    assert flow.stmts[0].runnable == "helper"
    assert isinstance(flow.stmts[3], SeekStmt)
    assert flow.stmts[3].agent == "alice"
    assert isinstance(flow.stmts[5], AskStmt)
    assert flow.stmts[5].body == "continue?"
    assert isinstance(flow.stmts[6], ScatterStmt)
    assert flow.stmts[6].count == 3
    assert isinstance(flow.stmts[7], StormStmt)
    assert (flow.stmts[7].count, flow.stmts[7].par) == (4, 2)
    assert isinstance(flow.stmts[10], MapStmt)
    assert flow.stmts[10].par == 2
    assert isinstance(flow.stmts[11], KeepStmt)
    assert (flow.stmts[11].position, flow.stmts[11].count) == ("first", 2)
    assert isinstance(flow.stmts[13], DropStmt)
    assert (flow.stmts[13].position, flow.stmts[13].count) == ("last", 1)
    assert isinstance(flow.stmts[14], RankStmt)
    assert (flow.stmts[14].limit, flow.stmts[14].count, flow.stmts[14].par) == (
        "top",
        3,
        2,
    )


def test_inline_runnables_are_generated_once_and_referenced_by_name() -> None:
    program = Program.from_source(FLOW_SOURCE)
    flow = program.flows[0]
    generated = {agic.name: agic for agic in program.agics if agic.name.startswith("<agic:")}

    inline_run = flow.stmts[1]
    bare_run = flow.stmts[2]
    inline_seek = flow.stmts[4]
    repeat = flow.stmts[15]
    assert isinstance(inline_run, RunStmt)
    assert isinstance(bare_run, RunStmt)
    assert isinstance(inline_seek, SeekStmt)
    assert isinstance(repeat, RepeatStmt)
    assert inline_run.runnable in generated
    assert generated[inline_run.runnable].output == "Text"
    assert bare_run.runnable in generated
    assert generated[bare_run.runnable].messages[0].content == "bare inline run"
    assert inline_seek.runnable in generated
    assert repeat.until in generated
    assert generated[repeat.until].output == "Boolean"


def test_flow_bindings_are_independent_of_statement_kind() -> None:
    flow = Program.from_source(FLOW_SOURCE).flows[0]

    named = flow.stmts[-3]
    discarded = flow.stmts[-2]
    authored = flow.stmts[-1]
    assert isinstance(named, RunStmt) and named.binding == "result"
    assert isinstance(discarded, RunStmt) and discarded.binding is None
    assert isinstance(authored, LetStmt)
    assert (authored.binding, authored.value) == ("note", "authored text")


def test_inline_context_and_instruct_are_program_owned_declarations() -> None:
    program = Program.from_source(
        """
agic answer:
  context:
    Runtime context.
  instruct:
    Be concise.
  user: Answer now.
"""
    )
    agic = program.agics[0]

    assert agic.context == "<context:3>"
    assert agic.instruct == "<instruct:5>"
    assert program.contexts[0].name == agic.context
    assert program.contexts[0].body == "Runtime context."
    assert program.instructs[0].name == agic.instruct
    assert program.instructs[0].body == "Be concise."


def test_agic_and_flow_names_share_one_namespace() -> None:
    with pytest.raises(ToolangError, match="Duplicate runnable name"):
        Program.from_source(
            """
agic shared:
  Hello.

flow shared:
  run shared
"""
        )
