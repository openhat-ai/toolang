"""Flow contracts, inherited frames, and iteration history at execution boundaries.

New clauses are supplied as AST fields until the tree-sitter grammar is updated.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.execution_harness import ExecutionHarness, RecordingRunTracer
from toolang.execution.events import RunBegin
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.run import ModelCallResult
from toolang.execution.types import ThreadPrefix
from toolang.lang import Program
from toolang.lang.ast import RepeatStmt, SettleStmt


def _create(
    root: Path, *, source: str, responses: list[str], program: Program | None = None
) -> ExecutionHarness:
    return ExecutionHarness.create(
        root,
        source=source,
        program=program,
        responses=[
            ModelCallResult(message=Message.assistant(text)) for text in responses
        ],
    )


def _run(harness: ExecutionHarness, *, primary: str | None = None):
    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:main",
                    primary=(TextPart(primary),) if primary is not None else None,
                )
            )
            output = (
                harness.store.run_output_text(run_id=run.id)
                if run.output is not None
                else None
            )
            return (
                run,
                output,
                harness.store.resolve_error(run.error) if run.error else None,
            )

    return asyncio.run(scenario())


def _texts(harness: ExecutionHarness) -> list[str]:
    return [
        "\n".join(message_text(message.parts) for message in invocation.call.messages)
        for invocation in harness.adapter.invocations
    ]


@pytest.mark.parametrize(
    "operation,output",
    [
        ("map using transform", "Text[]"),
        ("keep if predicate", "Number[]"),
        ("drop if predicate", "Number[]"),
        ("sort ascending by score", "Number[]"),
        ("keep first 2", "Number[]"),
        ("drop last 2", "Number[]"),
    ],
)
def test_empty_lists_preserve_types_without_child_calls(
    tmp_path: Path, operation: str, output: str
) -> None:
    harness = _create(
        tmp_path,
        source=f"""
agic seed() -> Number[]:
  Seed.
agic transform -> Text:
  Transform.
agic predicate -> Boolean:
  Decide.
agic score -> Number:
  Score.
flow main() -> {output}:
  scatter 1 using seed
  {operation}
""",
        responses=["[]"],
    )
    expected_type = output
    run, output, error = _run(harness)
    assert run.status == "succeeded", run.error
    assert run.output is not None and run.output.local.type == expected_type
    assert output == "[]"
    assert len(harness.adapter.invocations) == 1


@pytest.mark.parametrize("operation", ["gather", "settle"])
def test_empty_reductions_fail_before_child_calls(
    tmp_path: Path, operation: str
) -> None:
    harness = _create(
        tmp_path,
        source=f"""
agic seed() -> Text[]:
  Seed.
agic reduce:
  Reduce.
flow main():
  scatter 1 using seed
  {operation} using reduce
""",
        responses=["[]"],
    )
    run, output, error = _run(harness)
    assert run.status == "failed"
    assert "nonempty list" in str(error)
    assert len(harness.adapter.invocations) == 1


def test_scatter_and_storm_can_omit_primary_and_keep_nested_arrays(
    tmp_path: Path,
) -> None:
    harness = _create(
        tmp_path,
        source="""
flow main() -> Text[][]:
  scatter 9 using:
    Make two strings.
  storm 2 in 1 lane using -> Text[]:
    Make a pair.
""",
        responses=['["ignored"]', '["a","b"]', '["c"]'],
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", run.error
    assert output == '[["a","b"],["c"]]'


def test_settle_uses_first_element_then_passes_current_and_previous_output(
    tmp_path: Path,
) -> None:
    harness = _create(
        tmp_path,
        source="""
agic seed() -> Text[]:
  Seed.
flow main():
  scatter 3 using seed
  settle using:
    current={{_}}; previous={{_1._}}
""",
        responses=['["a","b","c"]', "ab", "abc"],
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", run.error
    assert "current=b; previous=a" in _texts(harness)[1]
    assert "current=c; previous=ab" in _texts(harness)[2]
    assert output == "abc"


@pytest.mark.parametrize(
    "initial,output,responses,expected",
    [
        (None, "Text", ['["one"]'], "one"),
        ("start", "Text", ['["one"]', "start+one"], "start+one"),
        ("not a number", "Number", ['["one"]'], None),
        (None, "Number", ['["one"]'], None),
    ],
)
def test_settle_seed_contract_and_singleton(
    tmp_path: Path,
    initial: str | None,
    output: str,
    responses: list[str],
    expected: str | None,
) -> None:
    source = f"""
agic seed() -> Text[]:
  Seed.
agic reduce -> {output}:
  user: current={{{{_}}}}; previous={{{{_1._}}}}
flow main() -> {output}:
  scatter 1 using seed
  settle using reduce
"""
    program = Program.from_source(source)
    flow = program.flows[0]
    settle = flow.stmts[-1]
    assert isinstance(settle, SettleStmt)
    program = replace(
        program,
        flows=(
            replace(flow, stmts=(*flow.stmts[:-1], replace(settle, initial=initial))),
        ),
    )
    harness = _create(tmp_path, source=source, program=program, responses=responses)
    run, output, error = _run(harness)
    assert run.status == ("succeeded" if expected is not None else "failed"), run.error
    if expected is not None:
        assert output == expected
    assert len(harness.adapter.invocations) == len(responses)


@pytest.mark.parametrize("initial", [None, "initial"])
@pytest.mark.parametrize("items", ['["seed"]', '["seed","2","3"]'])
def test_settle_only_validates_consumed_elements_as_reducer_inputs(
    tmp_path: Path, initial: str | None, items: str
) -> None:
    source = """
agic seed() -> Text[]:
  Seed.
agic reduce(_: Number) -> Text:
  user: Add {{_}} to {{_1._}}.
flow main():
  scatter 3 using seed
  settle using reduce
"""
    program = Program.from_source(source)
    flow = program.flows[0]
    settle = flow.stmts[-1]
    assert isinstance(settle, SettleStmt)
    program = replace(
        program,
        flows=(
            replace(flow, stmts=(*flow.stmts[:-1], replace(settle, initial=initial))),
        ),
    )
    harness = _create(
        tmp_path,
        source=source,
        program=program,
        responses=[items, "seed+2", "seed+2+3"],
    )
    run, output, error = _run(harness)
    if initial is not None:
        assert run.status == "failed"
        assert "Number" in str(error)
        assert len(harness.adapter.invocations) == 1
    else:
        assert run.status == "succeeded", error
        singleton = items == '["seed"]'
        assert output == ("seed" if singleton else "seed+2+3")
        assert len(harness.adapter.invocations) == (1 if singleton else 3)


def test_until_waits_for_its_own_history_depth_and_reads_entry_exit(
    tmp_path: Path,
) -> None:
    harness = _create(
        tmp_path,
        source="""
flow main -> Text:
  repeat 5 times:
    run: Improve {{_}}.
    until:
      now={{_}}; previous={{_1._}}; older={{_2._}}; entry={{_2.__}}
""",
        responses=["a", "b", "c", "true"],
    )
    run, output, error = _run(harness, primary="seed")
    assert run.status == "succeeded", run.error
    assert len(harness.adapter.invocations) == 4
    assert "now=c; previous=b; older=a; entry=seed" in _texts(harness)[-1]
    assert output == "c"


def test_repeat_history_stays_fixed_across_body_statements(tmp_path: Path) -> None:
    harness = _create(
        tmp_path,
        source="""
flow main:
  repeat 2 times:
    run: first={{_}}; {{#_1}}previous={{_1._}}{{/_1}}
    run: second={{_}}; {{#_1}}previous={{_1._}}; entered={{_1.__}}{{/_1}}
""",
        responses=["a", "b", "c", "d"],
    )
    run, output, error = _run(harness, primary="seed")
    assert run.status == "succeeded", run.error
    texts = _texts(harness)
    assert "first=b; previous=b" in texts[2]
    assert "second=c; previous=b; entered=seed" in texts[3]


def test_nested_repeat_shadows_then_restores_outer_history(tmp_path: Path) -> None:
    harness = _create(
        tmp_path,
        source="""
flow main:
  repeat 2 times:
    repeat 1 time:
      run: inner={{_}}; {{^_1}}no history{{/_1}}
    run: outer={{_}}; {{#_1}}previous={{_1._}}{{/_1}}
""",
        responses=["a", "b", "c", "d"],
    )
    run, output, error = _run(harness, primary="seed")
    assert run.status == "succeeded", run.error
    assert "inner=b; no history" in _texts(harness)[2]
    assert "outer=c; previous=b" in _texts(harness)[3]


def test_until_window_is_local_and_out_of_window_is_an_error(tmp_path: Path) -> None:
    source = """
flow main:
  repeat 1 time:
    run: Improve {{_}}.
    until: {{#_2}}true{{/_2}}
"""
    program = Program.from_source(source)
    repeat = program.flows[0].stmts[0]
    assert isinstance(repeat, RepeatStmt)
    program = replace(
        program, flows=(replace(program.flows[0], stmts=(replace(repeat, window=1),)),)
    )
    harness = _create(tmp_path, source=source, program=program, responses=["a"])
    run, output, error = _run(harness, primary="seed")
    assert run.status == "failed"
    assert "outside the active window" in str(error)
    assert harness.adapter.invocations == []


def test_inherited_until_template_adds_history_requirement(tmp_path: Path) -> None:
    source = """
instruct condition:
  Compare against {{_2._}}.
flow main:
  repeat 4 times:
    let note = unchanged
    until: Return true.
"""
    program = Program.from_source(source)
    program = replace(program, flows=(replace(program.flows[0], instruct="condition"),))
    harness = _create(tmp_path, source=source, program=program, responses=["true"])
    run, output, error = _run(harness, primary="seed")
    assert run.status == "succeeded", run.error
    assert len(harness.adapter.invocations) == 1
    assert "Compare against seed." in harness.adapter.invocations[0].call.instructions


@pytest.mark.parametrize("child_lanes", [None, 5])
def test_lane_defaults_inherit_and_statement_override_is_local(
    tmp_path: Path, child_lanes: int | None
) -> None:
    from toolang.lang.ast import Directive, Span

    source = """
agic worker():
  Work.
flow child() -> Text[]:
  storm 1 in 1 lane using worker
  storm 3 using worker
flow main() -> Text[][]:
  storm 1 using child
"""
    program = Program.from_source(source)
    child, main = program.flows
    main = replace(
        main,
        directives=(
            Directive(name="lanes", operator="=", values=("2",), span=Span(line=1)),
        ),
    )
    if child_lanes is not None:
        child = replace(
            child,
            directives=(
                Directive(
                    name="lanes",
                    operator="=",
                    values=(str(child_lanes),),
                    span=Span(line=1),
                ),
            ),
        )
    harness = _create(
        tmp_path,
        source=source,
        program=replace(program, flows=(child, main)),
        responses=["a", "b", "c", "d"],
    )

    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="main"), tracer=tracer
            )
            assert run.status == "succeeded", run.error
            children = [
                event
                for event in tracer.events
                if isinstance(event, RunBegin) and event.parent is not None
            ]
            inherited = child_lanes or 2
            assert [
                child.occurrence.lane.count
                for child in children
                if child.occurrence and child.occurrence.lane
            ] == [2, 1, inherited, inherited, inherited]

    asyncio.run(scenario())


def test_child_recall_can_override_flow_none_without_automatic_history(
    tmp_path: Path,
) -> None:
    harness = _create(
        tmp_path,
        source="""
agic seed():
  context: none
  user: old question
agic worker(note):
  recall = near
  context: none
  user: flow={{note}}; history={{_past}}
flow main():
  recall = none
  let note = {{_near}}
  run worker
""",
        responses=["old answer", "done"],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            await harness.executor.run(harness.run_spec(thread=thread, runnable="seed"))
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="main")
            )
            assert run.status == "succeeded", run.error
            messages = harness.adapter.invocations[-1].call.messages
            assert len(messages) == 1
            text = message_text(messages[0].parts)
            assert "flow=[]; history=[" in text
            assert "old question" in text and "old answer" in text
            assert '"role":"user"' in text

    asyncio.run(scenario())


def test_inherited_instruct_cannot_capture_undeclared_parent_parameters(
    tmp_path: Path,
) -> None:
    from toolang.base.types.run import ToolCall

    source = """
instruct secret:
  Use {{topic}}.
agic parent(topic):
  hands = worker
  instruct: secret
  user: Delegate.
agic worker():
  user: Work.
"""
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall("child", "child", "_toolang__run", {"runnable": "worker"}),
                )
            ),
            ModelCallResult(message=Message.assistant("handled")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="parent", named={"topic": "private"}
                )
            )
            assert run.status == "succeeded", run.error
            child = next(
                item
                for item in harness.store.list_run_tree(root_run_id=run.id)
                if item.parent
            )
            assert child.status == "failed"
            assert child.error is not None
            assert "template input is missing: topic" in str(
                harness.store.resolve_error(child.error)
            )
            assert len(harness.adapter.invocations) == 2

    asyncio.run(scenario())


def test_settle_initializer_reads_outer_history_before_shadowing(
    tmp_path: Path,
) -> None:
    source = """
agic seed() -> Text[]:
  Seed.
flow main():
  repeat 2 times:
    scatter 1 using seed
    settle using:
      current={{_}}; seed={{_1._}}
"""
    program = Program.from_source(source)
    flow = program.flows[0]
    repeat = flow.stmts[0]
    assert isinstance(repeat, RepeatStmt)
    settle = repeat.stmts[1]
    assert isinstance(settle, SettleStmt)
    initial = "{{#_1}}{{_1._}}{{/_1}}{{^_1}}start{{/_1}}"
    repeat = replace(repeat, stmts=(repeat.stmts[0], replace(settle, initial=initial)))
    program = replace(program, flows=(replace(flow, stmts=(repeat,)),))
    harness = _create(
        tmp_path,
        source=source,
        program=program,
        responses=['["a"]', "first", '["b"]', "second"],
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", error
    assert "current=a; seed=start" in _texts(harness)[1]
    assert "current=b; seed=first" in _texts(harness)[3]
    assert output == "second"


def test_settle_accepts_array_valued_initializer_and_output(tmp_path: Path) -> None:
    source = """
agic seed() -> Text[]:
  Seed.
agic fold -> Number[]:
  user: Merge {{_}} into {{_1._}}.
flow main() -> Number[]:
  scatter 1 using seed
  settle using fold
"""
    program = Program.from_source(source)
    flow = program.flows[0]
    settle = flow.stmts[1]
    assert isinstance(settle, SettleStmt)
    program = replace(
        program,
        flows=(replace(flow, stmts=(flow.stmts[0], replace(settle, initial="[1,2]"))),),
    )
    harness = _create(
        tmp_path, source=source, program=program, responses=['["a"]', "[1,2,3]"]
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", error
    assert "Merge a into [1,2]." in _texts(harness)[1]
    assert output == "[1,2,3]"
