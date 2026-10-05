"""Complete array values cross runnable boundaries without dimensional flags."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.execution.types import (
    ControlRef,
    FieldRef,
    LoopStepNoted,
    ThreadPrefix,
    TypedRef,
)
from toolang.lang.errors import ToolangError


@pytest.mark.parametrize("target", ["named", "inline"])
@pytest.mark.parametrize(
    "type_name,value",
    [
        ("Text", "value"),
        ("Text[]", []),
        ("Text[]", ["a", "b"]),
        ("Text[][]", [["a"], []]),
    ],
)
def test_run_preserves_one_call_with_complete_input_and_output(
    tmp_path, target, type_name, value
):
    statement = "run echo" if target == "named" else f"run -> {type_name}: {{{{_}}}}"
    harness = ExecutionHarness.create(
        tmp_path,
        source=(
            f"agic echo(_: {type_name}) -> {type_name}:\n  {{{{_}}}}\n"
            f"flow main(_: {type_name}) -> {type_name}:\n  {statement}\n"
        ),
        responses=[
            ModelCallResult(
                message=Message.assistant(
                    value if type_name == "Text" else json.dumps(value)
                )
            )
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="main", named={"_": value})
            )
            assert run.status == "succeeded", run.error
            children = [
                child
                for child in harness.store.list_run_tree(root_run_id=run.id)
                if child.parent is not None
            ]
            assert len(children) == len(harness.adapter.invocations) == 1
            control = harness.store.get_run_control(run_id=children[0].id, index=0)
            assert control is not None
            source = TypedRef(
                FieldRef.from_path(control.ref, "payload", "input", "_"), type_name
            )
            assert run.output is not None and run.output.type == type_name
            assert harness.store.resolve_value(run.output.value) == (
                harness.store.resolve_value(source)
            )
            assert not harness.store.list_steps(run_id=run.id)[0].noted

    asyncio.run(scenario())


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
                assert run.output is not None and run.output.type == result_type
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
        "reduce -> Json:\n    {{_}}\n    from: []",
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
            assert run.output.type == "Part[]"
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
            assert harness.store.resolve_value(run.output.value) == item
            step = harness.store.list_steps(run_id=run.id)[0]
            assert step.output is not None
            assert isinstance(step.output.value, TypedRef)
            seed = FieldRef.from_path(
                ControlRef.for_run(run.id, 0), "payload", "input", "_", "!", 0
            )
            assert harness.store.resolve_value_pointer(step.output.value) == seed
            assert isinstance(run.output.value, TypedRef)
            assert step.output.type == run.output.type == "Json"
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
            assert retried.output is not None and retried.output.type == "Text[][]"
            assert json.loads(harness.store.run_output_text(run_id=run.id)) == [
                ["kept"]
            ]
            assert len(harness.adapter.invocations) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "type_name,value",
    [
        ("Json", None),
        ("Json", "kept"),
        ("Json", [["a"], []]),
        ("Text[][]", [["a", "b"], []]),
    ],
)
def test_retry_preserves_committed_output_when_later_result_is_discarded(
    tmp_path, type_name, value
):
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"flow main() -> {type_name}:\n  run -> {type_name}: Value\n  let run: Discard\n",
        responses=[
            ModelCallResult(message=Message.assistant(json.dumps(value))),
            RuntimeError("temporary failure"),
            ModelCallResult(message=Message.assistant("discarded")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="main")
            )
            assert run.status == "failed"
            committed = harness.store.list_steps(run_id=run.id)[0]
            assert committed.output is not None
            assert isinstance(committed.output.value, TypedRef)
            source = harness.store.resolve_value_pointer(committed.output.value)

            retried = await harness.executor.retry(
                run.id, setup=harness.setup, state=harness.state
            )
            assert retried.status == "succeeded", retried.error
            assert retried.output is not None
            assert retried.output.type == type_name
            assert isinstance(retried.output.value, TypedRef)
            assert harness.store.resolve_value_pointer(retried.output.value) == source
            assert harness.store.resolve_value(retried.output.value) == (
                harness.store.resolve_value(committed.output.value)
            )
            assert len(harness.adapter.invocations) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["named", "inline"])
@pytest.mark.parametrize("origin", ["parameter", "generate"])
@pytest.mark.parametrize(
    "output_type,initial,expected",
    [
        ("Text", "seed", "seed"),
        ("Number", 7, "7"),
        ("Number[][]", [[1, 2], []], "[[1,2],[]]"),
        ("Json", None, "null"),
    ],
)
def test_empty_reduce_returns_typed_initializer_without_child_calls(
    tmp_path, target, origin, output_type, initial, expected
):
    statement = (
        "reduce using combine" if target == "named" else f"reduce -> {output_type}"
    )
    body = "" if target == "named" else "    {{_}} {{_1._}}\n"
    prefix = "  generate 0: No calls\n" if origin == "generate" else ""
    initializer = "null" if initial is None else "{{initial}}"
    harness = ExecutionHarness.create(
        tmp_path,
        source=(
            f"agic combine(_: Number) -> {output_type}:\n  {{{{_}}}} {{{{_1._}}}}\n"
            f"flow main(_: Text[], initial: {output_type}) -> {output_type}:\n"
            f"{prefix}  {statement}:\n{body}    from: {initializer}\n"
        ),
        responses=[],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="main", named={"_": [], "initial": initial}
                )
            )
            assert run.status == "succeeded", (
                harness.store.resolve_error(run.error) if run.error else None
            )
            assert run.output is not None and run.output.type == output_type
            assert harness.store.run_output_text(run_id=run.id) == expected
            step = harness.store.list_steps(run_id=run.id)[-1]
            assert step.status == "succeeded"
            assert step.output is not None and step.output.type == output_type
            assert step.noted == LoopStepNoted(0, "exhausted", 0)
            assert len(harness.store.list_run_tree(root_run_id=run.id)) == 1
            assert not harness.adapter.invocations

    asyncio.run(scenario())
