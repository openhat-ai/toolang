"""Published grammar changes retain their meaning through execution and storage."""

import asyncio
import json

import pytest

from tests.support.execution_assertions import without_runtime_snapshots
from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, message_text
from toolang.base.types.run import ModelCallResult
from toolang.execution.store import RunStore
from toolang.execution.types import LoopStepReport, ThreadPrefix
from toolang.lang import format_source
from toolang.lang.ast import RunStmt


def _answer(text):
    return ModelCallResult(message=Message.assistant(text))


def _prompt(invocation):
    return message_text(
        without_runtime_snapshots(invocation.call.messages)[-1].parts
    ).split("</toolang:context>\n\n", 1)[-1]


@pytest.mark.parametrize("entry", ["let", "inline", "message", "prompt", "initial"])
@pytest.mark.parametrize("ending", ["\n", "\r\n"])
@pytest.mark.parametrize("formatted", [False, True])
def test_literal_hashes_and_quotes_reach_outputs_and_prompts(
    tmp_path, entry, ending, formatted
):
    literal = 'Visit https://example.com/#details with "a#b" # still text.'
    sources = {
        "let": f"flow main() -> Text:\n  let note = {literal}\n  run echo\n"
        "agic echo(note: Part[]) -> Text:\n  user: {{note}}\n",
        "inline": f"flow main():\n  run: {literal}\n",
        "message": f"agic main():\n  user: {literal}\n",
        "prompt": f"prompt note:\n  {literal}\nagic main():\n  user: $note\n",
        "initial": f"flow main(_: Text[]) -> Text:\n  reduce using combine:\n    initial: {literal}\n"
        "agic combine(_: Text) -> Text:\n  {{_}} {{_1._}}\n",
    }
    source = sources[entry].replace("\n", ending)
    if formatted:
        source = format_source(source)
    calls = entry != "initial"
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[_answer("done")] if calls else [],
        prepare_state=entry == "prompt",
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.CHAT),
                    runnable="main",
                    named={"_": []} if entry == "initial" else {},
                )
            )
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            assert harness.store.run_output_text(run_id=root.id) == (
                "done" if calls else literal
            )
            assert len(harness.adapter.invocations) == int(calls)
            if calls:
                assert _prompt(harness.adapter.invocations[0]) == literal
            assert harness.adapter.pending_responses == 0
            reopened = RunStore(harness.store.db_path, read_only=True)
            try:
                assert reopened.run_output_text(run_id=root.id) == (
                    "done" if calls else literal
                )
                assert reopened.list_steps(run_id=root.id) == harness.store.list_steps(
                    run_id=root.id
                )
            finally:
                reopened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "clause,count", [("00 time", 0), ("01 times", 1), ("02 time", 2)]
)
@pytest.mark.parametrize("target", ["named", "inline"])
def test_repeat_units_and_first_member_docs_preserve_iteration_counts(
    tmp_path, clause, count, target
):
    call = "worker" if target == "named" else "-> Text: Work {{_}} # literal."
    source = (
        "agic worker(_: Text) -> Text:\n  user: Work {{_}} # literal.\n"
        "flow main(_: Text) -> Text:\n"
        "  recall = none\n  context = none\n  instruct = none\n"
        f"  repeat {clause}:\n    ## Work.\n    run {call}\n"
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[_answer(f"round {index}") for index in range(count)],
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.CHAT),
                    runnable="main",
                    named={"_": "seed"},
                )
            )
            assert root.status == "succeeded", root.error
            assert harness.store.run_output_text(run_id=root.id) == (
                f"round {count - 1}" if count else "seed"
            )
            steps = harness.store.list_steps(run_id=root.id)
            assert steps[0].report == LoopStepReport(count, "exhausted", count)
            for step in steps[1:]:
                assert isinstance(step.spec, RunStmt)
                assert step.spec.doc == "Work."
            assert [_prompt(call) for call in harness.adapter.invocations] == [
                f"Work {'seed' if index == 0 else f'round {index - 1}'} # literal."
                for index in range(count)
            ]
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["sort", "using", "until", "run"])
@pytest.mark.parametrize("mode", ["run", "async", "exec"])
@pytest.mark.parametrize("target", ["named", "inline"])
def test_contextual_names_bind_child_inputs_and_retained_handles(
    tmp_path, name, mode, target
):
    literal = 'Keep # literal "quotes".'
    body = f"Received {{{{{name}}}}}."
    reference = "worker" if target == "named" else f"-> Text: {body}"
    call = {
        "run": f"run {reference}",
        "async": f"let until = async run {reference}\n  await until",
        "exec": f"exec {reference}",
    }[mode]
    source = (
        f"agic worker({name}: Text) -> Text:\n  recall = none\n  user: {body}\n"
        f"flow main({name}: Text) -> Text:\n"
        "  recall = none\n  context = none\n  instruct = none\n"
        f"  let {name} = {{{{{name}}}}}\n  {call}\n"
    )
    harness = ExecutionHarness.create(
        tmp_path, source=source, responses=[_answer("done")]
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.CHAT),
                    runnable="main",
                    named={name: literal},
                )
            )
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            assert harness.store.run_output_text(run_id=root.id) == "done"
            assert len(harness.adapter.invocations) == 1
            assert _prompt(harness.adapter.invocations[0]) == f"Received {literal}."
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["generate", "map", "keep", "drop", "sort"])
@pytest.mark.parametrize("clause,lanes", [("01 lanes", 1), ("02 lane", 2)])
@pytest.mark.parametrize("target", ["named", "inline"])
def test_lane_prefixes_reach_child_occurrences_and_typed_collection_results(
    tmp_path, operation, clause, lanes, target
):
    predicate = operation in {"keep", "drop"}
    output = "Boolean" if predicate else "Number" if operation == "sort" else "Text"
    connector = "if" if predicate else "by" if operation == "sort" else "using"
    head = (
        "generate 3"
        if operation == "generate"
        else ("sort ascending" if operation == "sort" else operation)
    )
    call = (
        f"{connector} worker"
        if target == "named"
        else f"{connector if predicate or operation == 'sort' else ''} -> {output}: Item {{{{_}}}}."
    )
    primary = "Text" if operation == "generate" else "Text[]"
    source = (
        f"agic worker(_: Text) -> {output}:\n  user: Item {{{{_}}}}.\n"
        f"flow main(_: {primary}) -> Text[]:\n  lanes = 3\n"
        f"  {head} in {clause} {call}\n"
    )
    responses = (
        ["true", "false", "true"]
        if predicate
        else (["2", "1", "3"] if operation == "sort" else ["A", "B", "C"])
    )
    expected = {
        "generate": ["A", "B", "C"],
        "map": ["A", "B", "C"],
        "keep": ["a", "c"],
        "drop": ["b"],
        "sort": ["b", "a", "c"],
    }[operation]
    harness = ExecutionHarness.create(
        tmp_path, source=source, responses=[_answer(text) for text in responses]
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.CHAT),
                    runnable="main",
                    named={"_": "seed" if operation == "generate" else ["a", "b", "c"]},
                )
            )
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            assert root.output is not None and root.output.type == "Text[]"
            assert json.loads(harness.store.run_output_text(run_id=root.id)) == expected
            children = harness.store.list_run_tree(root_run_id=root.id)[1:]
            assert len(children) == len(harness.adapter.invocations) == 3
            for index, child in enumerate(children):
                assert child.occur is not None
                assert child.occur.item is not None and child.occur.item.index == index
                assert child.occur.lane is not None
                assert child.occur.lane.count == lanes
                assert 0 <= child.occur.lane.index < lanes
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())
