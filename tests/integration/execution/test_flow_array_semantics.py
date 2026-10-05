"""Complete array values cross runnable boundaries without dimensional flags."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.execution.types import ThreadPrefix
from toolang.lang.errors import ToolangError


@pytest.mark.parametrize("type_name", ["Text[][]", "Json"])
@pytest.mark.parametrize("origin", ["parameter", "run", "helper", "exec"])
@pytest.mark.parametrize("size", [0, 1, 2])
@pytest.mark.parametrize("operation", ["map", "keep", "drop", "sort", "reduce"])
def test_outer_arrays_are_identical_across_boundaries(
    tmp_path: Path, type_name: str, origin: str, size: int, operation: str
) -> None:
    items = [["a", "b"], ["c"]][:size]
    element = "Text[]" if type_name == "Text[][]" else "Json"
    statements = {
        "map": f"map in 1 lane -> {element}: {{{{_}}}}",
        "keep": "keep first 1",
        "drop": "drop first 1",
        "sort": "sort ascending in 1 lane by: {{_}}",
        "reduce": f"reduce -> {element}: {{{{_}}}} {{{{_1._}}}}",
    }
    expected = {
        "map": items,
        "keep": items[:1],
        "drop": items[1:],
        "sort": list(reversed(items)),
        "reduce": items[-1] if items else None,
    }[operation]
    answers = {
        "map": [json.dumps(item) for item in items],
        "keep": [],
        "drop": [],
        "sort": [str(size - i) for i in range(size)],
        "reduce": [json.dumps(item) for item in items[1:]],
    }[operation]
    prefix = {
        "parameter": "",
        "run": f"run -> {type_name}: Items\n  ",
        "helper": "run helper\n  ",
        "exec": "exec helper",
    }[origin]
    result_type = (
        element
        if operation == "reduce"
        else f"{element}[]"
        if operation == "map"
        else type_name
    )
    helper = (
        f"flow helper(_: {type_name}) -> {result_type}:\n  {statements[operation]}\n"
        if origin == "exec"
        else f"flow helper() -> {type_name}:\n  run -> {type_name}: Items\n"
    )
    source = (
        helper
        + f"flow main(_: {type_name}) -> {result_type}:\n  lanes = 1\n  "
        + prefix
    )
    if origin != "exec":
        source += statements[operation]
    source += "\n"
    responses = ([json.dumps(items)] if origin in {"run", "helper"} else []) + answers
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[
            ModelCallResult(message=Message.assistant(answer)) for answer in responses
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="main", named={"_": items})
            )
            if not size and operation == "reduce":
                assert run.status == "failed"
                assert run.error is not None
                assert (
                    harness.store.resolve_error(run.error)
                    == "reduce requires a nonempty array"
                )
            else:
                assert run.status == "succeeded", (
                    harness.store.resolve_error(run.error) if run.error else None
                )
                assert run.output is not None and run.output.local.type == result_type
                actual = harness.store.run_output_text(run_id=run.id)
                assert json.loads(actual) == expected
            assert len(harness.adapter.invocations) == len(responses)
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [None, {}, "[]", "hello", 1, False])
@pytest.mark.parametrize(
    "statement",
    [
        "map: {{_}}",
        "keep first 1",
        "drop last 1",
        "sort ascending by: {{_}}",
        "reduce -> Json: {{_}}",
    ],
)
def test_open_json_nonarrays_fail_inside_step_without_child_calls(
    tmp_path, value, statement
):
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"flow main() -> Json:\n  run -> Json: Value\n  {statement}\n",
        responses=[ModelCallResult(message=Message.assistant(json.dumps(value)))],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="main")
            )
            assert run.status == "failed" and run.error is not None
            assert "requires an outer array" in harness.store.resolve_error(run.error)
            steps = harness.store.list_steps(run_id=run.id)
            assert len(steps) == 2 and steps[-1].status == "failed"
            assert len(harness.adapter.invocations) == 1
            assert len(harness.store.list_run_tree(root_run_id=run.id)) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("statement", ["keep first 1", "drop last 1"])
def test_part_arrays_select_outer_parts_and_keep_their_type(tmp_path, statement):
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"flow main(_: Part[]) -> Part[]:\n  {statement}\n",
        responses=[],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="main",
                    primary=(TextPart("first"), TextPart("second")),
                )
            )
            assert run.status == "succeeded" and run.output is not None
            assert run.output.local.type == "Part[]"
            assert harness.store.run_output_text(run_id=run.id) == "first"
            assert not harness.adapter.invocations

    asyncio.run(scenario())


def test_definitely_missing_collection_input_is_rejected():
    from toolang.lang import Program

    with pytest.raises(ToolangError, match="outer array, got missing input"):
        Program.from_source(
            "agic worker(_):\n  Work.\nflow main():\n  map using worker\n"
        )


@pytest.mark.parametrize("item", [None, "null", "[]", "hello", False, 0, {"a": 1}])
def test_singleton_json_reduce_keeps_the_seed_without_a_child_call(tmp_path, item):
    harness = ExecutionHarness.create(
        tmp_path,
        source="flow main(_: Json) -> Json:\n  reduce -> Json: {{_}} {{_1._}}\n",
        responses=[],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="main", named={"_": [item]})
            )
            assert run.status == "succeeded", (
                harness.store.resolve_error(run.error) if run.error else None
            )
            assert run.output is not None
            assert harness.store.resolve_value(run.output.local.value) == item
            assert not harness.adapter.invocations

    asyncio.run(scenario())


def test_retry_restores_nested_arrays_and_selected_item_provenance(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="flow main(_: Text[][]) -> Text[][]:\n  keep first 1\n  map in 1 lane -> Text[]: {{_}}\n",
        responses=[
            RuntimeError("temporary failure"),
            ModelCallResult(message=Message.assistant('["kept"]')),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="main", named={"_": [["a", "b"], ["c"]]}
                )
            )
            assert run.status == "failed"
            retried = await harness.executor.retry(
                run.id, setup=harness.setup, state=harness.state
            )
            assert retried.status == "succeeded", retried.error
            assert (
                retried.output is not None and retried.output.local.type == "Text[][]"
            )
            assert json.loads(harness.store.run_output_text(run_id=run.id)) == [
                ["kept"]
            ]
            assert len(harness.adapter.invocations) == 2

    asyncio.run(scenario())
