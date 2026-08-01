"""Flow correctness scenarios composed from public execution objects."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.support.execution_assertions import (
    assert_run_event_integrity,
    event_labels,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingTool,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.run import ModelCallResult
from toolang.execution.events import RunBegin, RunEnd
from toolang.execution.types import ThreadPrefix
from toolang.lang.input import perceive_input


def _output_value(harness: ExecutionHarness, run_id: str) -> object:
    return json.loads(harness.store.run_output_text(run_id=run_id))


def _root_step_kinds(
    harness: ExecutionHarness,
    run_id: str,
) -> list[str]:
    return [
        step.kind
        for step in harness.store.list_steps(run_id=run_id)
        if step.parent == run_id
    ]


def test_flow_calls_an_agic_as_a_nested_run(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic echo(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow relay(_: Part[]) -> Part[]:
  run echo
""",
        responses=[ModelCallResult(message=Message.assistant("relayed"))],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="relay",
                    input=perceive_input("hello"),
                ),
                tracer=tracer,
            )

            runs = harness.store.list_runs(thread_id=thread, limit=None)
            children = [run for run in runs if run.parent is not None]
            assert root.status == "finished"
            assert len(children) == 1
            child = children[0]
            assert child.status == "finished"
            assert child.parent == f"{root.id}/0"
            assert child.root_run_id == root.id
            assert harness.store.run_output(run_id=root.id) == (
                TextPart("relayed"),
            )
            assert _root_step_kinds(harness, root.id) == ["run"]
            assert [
                step.kind
                for step in harness.store.list_steps(run_id=child.id)
            ] == ["model"]
            run_events = [
                event
                for event in tracer.events
                if isinstance(event, RunBegin | RunEnd)
            ]
            assert [(event.type, event.run) for event in run_events] == [
                ("run_begin", root.id),
                ("run_begin", child.id),
                ("run_end", child.id),
                ("run_end", root.id),
            ]

    asyncio.run(scenario())


def test_parallel_flow_failure_terminates_every_started_child(
    tmp_path: Path,
) -> None:
    gates = [AsyncGate() for _ in range(3)]
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow parallel(_: Part[]):
  storm 3 worker par 3
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(
                    message=Message.assistant(f"item {index}")
                ),
                gate=gate,
            )
            for index, gate in enumerate(gates)
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="parallel",
                    input=perceive_input("work"),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(
                asyncio.gather(
                    *(gate.wait_until_entered() for gate in gates)
                ),
                timeout=1,
            )
            gates[0].fail(RuntimeError("worker failed"))
            root = await asyncio.wait_for(handle, timeout=2)

            runs = harness.store.list_runs(thread_id=thread, limit=None)
            children = [run for run in runs if run.parent is not None]
            assert root.status == "failed"
            assert len(children) == 3
            assert sorted(run.status for run in children) == [
                "canceled",
                "canceled",
                "failed",
            ]
            assert all(
                step.status != "running"
                for run in runs
                for step in harness.store.list_steps(run_id=run.id)
            )
            assert [
                (step.kind, step.status)
                for step in harness.store.list_steps(run_id=root.id)
            ] == [("par", "failed")]
            assert_run_event_integrity(tracer.events)
            assert sum(
                isinstance(event, RunBegin) for event in tracer.events
            ) == 4
            assert sum(
                isinstance(event, RunEnd) for event in tracer.events
            ) == 4

    asyncio.run(scenario())


def test_runtime_failure_outside_a_step_emits_a_failed_system_step(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow fail(_: Part[]) -> Number:
  let note:
    captured
""",
        responses=[],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="fail",
                    input=perceive_input("not a number"),
                ),
                tracer=tracer,
            )

            assert record.status == "failed"
            assert record.error is not None
            assert record.error.startswith("output is not valid Number")
            steps = harness.store.list_steps(run_id=record.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("system", "finished"),
                ("system", "failed"),
            ]
            assert steps[-1].given == {"runtime": "failure"}
            assert steps[-1].output == (TextPart(record.error),)
            assert_run_event_integrity(tracer.events)
            assert event_labels(tracer.events) == [
                f"run_begin:{record.id}",
                f"step_begin:{record.id}/0:system",
                f"step_end:{record.id}/0:system:finished",
                f"step_begin:{record.id}/1:system",
                f"step_end:{record.id}/1:system:failed",
                f"run_end:{record.id}:failed",
            ]

    asyncio.run(scenario())


def test_scatter_then_map_preserves_item_order_and_types(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

agic upper(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow mapped(_: Text) -> Text[]:
  scatter 2 split
  map upper par 2
""",
        responses=[
            ModelCallResult(message=Message.assistant('["one","two"]')),
            ModelCallResult(message=Message.assistant("ONE")),
            ModelCallResult(message=Message.assistant("TWO")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="mapped",
                    input=perceive_input("split this"),
                )
            )

            assert root.status == "finished"
            assert _output_value(harness, root.id) == ["ONE", "TWO"]
            assert _root_step_kinds(harness, root.id) == ["run", "par"]
            assert [
                invocation.call.messages[-1]
                for invocation in harness.adapter.invocations[1:]
            ] == [Message.user("one"), Message.user("two")]
            children = [
                run
                for run in harness.store.list_runs(
                    thread_id=thread,
                    limit=None,
                )
                if run.parent == f"{root.id}/1"
            ]
            assert sorted(
                run.context["placement"]["item"] for run in children
            ) == [0, 1]

    asyncio.run(scenario())


def test_deep_search_example_uses_explicit_flow_reshaping(
    tmp_path: Path,
) -> None:
    source = (
        Path(__file__).parents[3] / "examples" / "deep_search.too"
    ).read_text(encoding="utf-8")
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[
            ModelCallResult(
                message=Message.assistant(
                    '["query 1","query 2","query 3","query 4","query 5","query 6"]'
                )
            ),
            *[
                ModelCallResult(message=Message.assistant(f"evidence {index}"))
                for index in range(6)
            ],
            *[
                ModelCallResult(
                    message=Message.assistant(
                        "true" if index % 2 == 0 else "false"
                    )
                )
                for index in range(6)
            ],
            *[
                ModelCallResult(message=Message.assistant(score))
                for score in ("3", "1", "2")
            ],
            *[
                ModelCallResult(message=Message.assistant(f"finding {index}"))
                for index in range(3)
            ],
            ModelCallResult(message=Message.assistant("report")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="research",
                    input=perceive_input("agent framework/sdk"),
                )
            )

            assert root.status == "finished", root.error
            assert harness.store.run_output_text(run_id=root.id) == "report"
            assert _root_step_kinds(harness, root.id) == [
                "system",
                "run",
                "par",
                "par",
                "par",
                "par",
                "run",
            ]
            root_steps = [
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.parent == root.id
            ]
            assert root_steps[3].noted["items"] == 3
            assert root_steps[4].noted["items"] == 3
            assert len(harness.adapter.invocations) == 20
            predicate_messages = [
                message_text(invocation.call.messages[-1].parts)
                for invocation in harness.adapter.invocations[7:13]
            ]
            assert all(
                len(invocation.call.messages) == 1
                for invocation in harness.adapter.invocations[7:13]
            )
            assert all(
                "Research question:\nagent framework/sdk" in message
                for message in predicate_messages
            )
            final_message = harness.adapter.invocations[-1].call.messages[-1]
            assert any(
                isinstance(part, TextPart)
                and "Research question:\nagent framework/sdk" in part.text
                and "Findings:" in part.text
                for part in final_message.parts
            )

    asyncio.run(scenario())


def test_inline_rank_scorer_has_no_recalled_history_or_tools(
    tmp_path: Path,
) -> None:
    tool = RecordingTool("test__side_effect", output={"ok": True})
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic remember(_: Text) -> Text:
  recall = none
  context: none
  user: {{_}}

agic split(_: Text) -> Text[]:
  recall = none
  context: none
  user: {{_}}

flow select(_: Text) -> Text[]:
  scatter 1 split
  rank top 1: Return a numeric relevance score from 0 to 10.
""",
        tools={tool.name: tool},
        responses=[
            ModelCallResult(message=Message.assistant("history marker")),
            ModelCallResult(message=Message.assistant('["candidate"]')),
            ModelCallResult(message=Message.assistant("10")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            remembered = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="remember",
                    input=perceive_input("remember this"),
                )
            )
            selected = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="select",
                    input=perceive_input("candidate"),
                )
            )

            assert remembered.status == "finished"
            assert selected.status == "finished", selected.error
            assert _output_value(harness, selected.id) == ["candidate"]
            score_call = harness.adapter.invocations[-1].call
            assert score_call.tools == ()
            assert all(
                "history marker" not in message_text(message.parts)
                for message in score_call.messages
            )
            assert tool.calls == []

    asyncio.run(scenario())


def test_storm_honors_parallel_limit_and_preserves_result_order(
    tmp_path: Path,
) -> None:
    gates = [AsyncGate() for _ in range(3)]
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow fanout(_: Text) -> Text[]:
  storm 3 worker par 2
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(
                    message=Message.assistant(f"item {index}")
                ),
                gate=gate,
            )
            for index, gate in enumerate(gates)
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="fanout",
                    input=perceive_input("work"),
                )
            )
            await asyncio.wait_for(
                asyncio.gather(
                    gates[0].wait_until_entered(),
                    gates[1].wait_until_entered(),
                ),
                timeout=1,
            )
            assert not gates[2].entered

            gates[1].release()
            await asyncio.wait_for(gates[2].wait_until_entered(), timeout=1)
            gates[2].release()
            gates[0].release()
            root = await asyncio.wait_for(handle, timeout=2)

            assert root.status == "finished"
            assert _output_value(harness, root.id) == [
                "item 0",
                "item 1",
                "item 2",
            ]
            assert _root_step_kinds(harness, root.id) == ["par"]

    asyncio.run(scenario())


def test_scatter_then_gather_reshapes_the_list_to_one_item(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

agic merge(_: Text[]) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow summary(_: Text) -> Text:
  scatter 3 split
  gather merge
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b","c"]')),
            ModelCallResult(message=Message.assistant("a+b+c")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="summary",
                    input=perceive_input("summarize"),
                )
            )

            assert root.status == "finished"
            assert harness.store.run_output_text(run_id=root.id) == "a+b+c"
            assert _root_step_kinds(harness, root.id) == ["run", "run"]
            assert harness.adapter.invocations[-1].call.messages[-1] == (
                Message.user('["a","b","c"]')
            )

    asyncio.run(scenario())


def test_scatter_then_settle_carries_the_accumulator_sequentially(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

agic fold(_: Part[], item: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}{{item}}

flow folded(_: Text) -> Text:
  scatter 3 split
  settle fold
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b","c"]')),
            ModelCallResult(message=Message.assistant("a")),
            ModelCallResult(message=Message.assistant("ab")),
            ModelCallResult(message=Message.assistant("abc")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="folded",
                    input=perceive_input("fold"),
                )
            )

            assert root.status == "finished"
            assert harness.store.run_output_text(run_id=root.id) == "abc"
            assert _root_step_kinds(harness, root.id) == ["run", "loop"]
            assert [
                invocation.call.messages[-1]
                for invocation in harness.adapter.invocations[1:]
            ] == [
                Message.user("a"),
                Message.user("ab"),
                Message.user("abc"),
            ]

    asyncio.run(scenario())


def test_inline_settle_receives_empty_accumulator_and_zero_based_items(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow folded(_: Text) -> Text:
  scatter 3 split
  settle -> Text:
    {{_}}{{item}}
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b","c"]')),
            ModelCallResult(message=Message.assistant("a")),
            ModelCallResult(message=Message.assistant("ab")),
            ModelCallResult(message=Message.assistant("abc")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="folded",
                    input=perceive_input("fold"),
                ),
                tracer=tracer,
            )

            assert root.status == "finished"
            assert harness.store.run_output_text(run_id=root.id) == "abc"
            assert [
                message_text(invocation.call.messages[-1].parts).rsplit("\n", 1)[-1]
                for invocation in harness.adapter.invocations[1:]
            ] == [
                "a",
                "ab",
                "abc",
            ]
            placements = [
                event.context["placement"]
                for event in tracer.events
                if isinstance(event, RunBegin)
                and event.parent is not None
                and "loop" in event.context.get("placement", {})
            ]
            assert placements == [
                {"item": 0, "items": 3, "loop": 0},
                {"item": 1, "items": 3, "loop": 1},
                {"item": 2, "items": 3, "loop": 2},
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("keep first 3", ["a", "b", "c"]),
        ("keep last 2", ["c", "d"]),
        ("drop first 3", ["d"]),
        ("drop last 2", ["a", "b"]),
    ],
)
def test_positional_keep_and_drop(
    tmp_path: Path,
    statement: str,
    expected: list[str],
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{{{_}}}}

flow selected(_: Text) -> Text[]:
  scatter 4 split
  {statement}
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b","c","d"]')),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="selected",
                    input=perceive_input("select"),
                )
            )

            assert root.status == "finished"
            assert _output_value(harness, root.id) == expected
            assert _root_step_kinds(harness, root.id) == ["run", "system"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("keep relevant par 2", ["a", "c"]),
        ("drop relevant par 2", ["b"]),
    ],
)
def test_predicate_keep_and_drop(
    tmp_path: Path,
    statement: str,
    expected: list[str],
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{{{_}}}}

agic relevant(_: Text) -> Boolean:
  recall = none
  context: none
  instruct: none
  user: {{{{_}}}}

flow selected(_: Text) -> Text[]:
  scatter 3 split
  {statement}
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b","c"]')),
            ModelCallResult(message=Message.assistant("true")),
            ModelCallResult(message=Message.assistant("false")),
            ModelCallResult(message=Message.assistant("true")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="selected",
                    input=perceive_input("select"),
                )
            )

            assert root.status == "finished"
            assert _output_value(harness, root.id) == expected
            assert _root_step_kinds(harness, root.id) == ["run", "par"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ("top 3", ["a", "b", "d"]),
        ("bottom 2", ["d", "c"]),
    ],
)
def test_rank_is_stable_and_applies_top_or_bottom_selection(
    tmp_path: Path,
    selection: str,
    expected: list[str],
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{{{_}}}}

agic score(_: Text) -> Number:
  recall = none
  context: none
  instruct: none
  user: {{{{_}}}}

flow ranked(_: Text) -> Text[]:
  scatter 4 split
  rank score {selection} par 2
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b","c","d"]')),
            ModelCallResult(message=Message.assistant("3")),
            ModelCallResult(message=Message.assistant("3")),
            ModelCallResult(message=Message.assistant("1")),
            ModelCallResult(message=Message.assistant("2")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="ranked",
                    input=perceive_input("rank"),
                )
            )

            assert root.status == "finished"
            assert _output_value(harness, root.id) == expected
            assert _root_step_kinds(harness, root.id) == ["run", "par"]

    asyncio.run(scenario())


def test_repeat_count_chains_each_iteration_output(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic echo(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow repeated(_: Text) -> Text:
  repeat 3:
    run echo
""",
        responses=[
            ModelCallResult(message=Message.assistant("one")),
            ModelCallResult(message=Message.assistant("two")),
            ModelCallResult(message=Message.assistant("three")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="repeated",
                    input=perceive_input("zero"),
                )
            )

            assert root.status == "finished"
            assert harness.store.run_output_text(run_id=root.id) == "three"
            assert _root_step_kinds(harness, root.id) == ["loop"]
            assert [
                invocation.call.messages[-1]
                for invocation in harness.adapter.invocations
            ] == [
                Message.user("zero"),
                Message.user("one"),
                Message.user("two"),
            ]

    asyncio.run(scenario())


def test_repeat_until_stops_before_the_count_limit(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic echo(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow repeated(_: Text) -> Text:
  repeat 5:
    run echo
    until:
      Return true when complete.
""",
        responses=[
            ModelCallResult(message=Message.assistant("one")),
            ModelCallResult(message=Message.assistant("false")),
            ModelCallResult(message=Message.assistant("two")),
            ModelCallResult(message=Message.assistant("true")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="repeated",
                    input=perceive_input("zero"),
                )
            )

            assert root.status == "finished"
            assert harness.store.run_output_text(run_id=root.id) == "two"
            assert _root_step_kinds(harness, root.id) == ["loop"]
            assert len(harness.adapter.invocations) == 4
            loop = next(
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.parent == root.id
            )
            assert loop.noted["shape"] == "item"
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("statement", "step_kind", "operation"),
    [
        ("map echo", "par", "map"),
        ("gather echo", "run", "gather"),
        ("settle echo", "loop", "settle"),
    ],
)
def test_list_statements_fail_inside_their_own_step_boundary(
    tmp_path: Path,
    statement: str,
    step_kind: str,
    operation: str,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
agic echo(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{{{_}}}}

flow invalid(_: Text) -> Text:
  {statement}
""",
        responses=[],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="invalid",
                    input=perceive_input("not a list"),
                ),
                tracer=tracer,
            )

            error = f"{operation} requires current shape list, got item"
            assert root.status == "failed"
            assert root.error == error
            steps = [
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.parent == root.id
            ]
            assert [(step.kind, step.status, step.error) for step in steps] == [
                (step_kind, "failed", error)
            ]
            assert harness.adapter.invocations == []
            assert_run_event_integrity(tracer.events)
            assert event_labels(tracer.events) == [
                f"run_begin:{root.id}",
                f"step_begin:{root.id}/0:{step_kind}",
                f"step_end:{root.id}/0:{step_kind}:failed",
                f"run_end:{root.id}:failed",
            ]

    asyncio.run(scenario())


def test_scatter_uses_the_returned_list_length_instead_of_authored_count(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow scattered(_: Text) -> Text[]:
  scatter 3 split
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b"]')),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="scattered",
                    input=perceive_input("split"),
                )
            )

            assert root.status == "finished"
            assert _output_value(harness, root.id) == ["a", "b"]
            root_steps = [
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.parent == root.id
            ]
            assert [(step.kind, step.status) for step in root_steps] == [
                ("run", "finished")
            ]
            assert root_steps[0].noted["items"] == 2
            child = next(
                run
                for run in harness.store.list_runs(
                    thread_id=thread,
                    limit=None,
                )
                if run.parent is not None
            )
            assert child.status == "finished"

    asyncio.run(scenario())


def test_recursive_run_forwards_named_arguments_and_primary_local(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic combine(_: Text, suffix: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}{{suffix}}

flow relay(_: Text, suffix: Text) -> Text:
  run combine
""",
        responses=[
            ModelCallResult(message=Message.assistant("hello!")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="relay",
                    input=perceive_input("hello"),
                    args={"suffix": "!"},
                )
            )

            assert root.status == "finished"
            assert harness.adapter.invocations[0].call.messages == [
                Message.user("hello!")
            ]
            child = next(
                run
                for run in harness.store.list_runs(
                    thread_id=thread,
                    limit=None,
                )
                if run.parent is not None
            )
            assert child.context["args"] == {"suffix": "!"}
            assert harness.store.run_output_text(run_id=root.id) == "hello!"

    asyncio.run(scenario())
