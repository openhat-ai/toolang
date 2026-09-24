"""Call-site contracts preserve defaults and reject incompatible child inputs."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.execution.types import ThreadPrefix


@pytest.mark.parametrize("via_flow", [False, True], ids=["root", "child"])
def test_declared_agic_defaults_persist_text_output(
    tmp_path: Path, via_flow: bool
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker:
  recall = none
  instruct = none
  context = none
  {{_}}
flow main:
  run worker
""",
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="main" if via_flow else "worker",
                    primary=(TextPart("input"),),
                )
            )
            runs = harness.store.list_run_tree(root_run_id=root.id)
            assert len(runs) == (2 if via_flow else 1)
            for run in runs:
                assert run.status == "succeeded", run.error
                assert run.output is not None and run.output.local.type == "Text"
                assert harness.store.run_output_text(run_id=run.id) == "done"
            assert len(harness.adapter.invocations) == 1
            assert harness.adapter.invocations[0].call.output_schema is None
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body,responses,child_types,result_type,result",
    [
        ("run: {{_}}", ["done"], ["Text"], "Text", "done"),
        ("scatter: Items", ['["a","b"]'], ["Text[]"], "Text[]", '["a","b"]'),
        ("storm 2 using: Work", ["a", "b"], ["Text", "Text"], "Text[]", '["a","b"]'),
        (
            "scatter: Items\n  map using: {{_}}",
            ['["a","b"]', "A", "B"],
            ["Text[]", "Text", "Text"],
            "Text[]",
            '["A","B"]',
        ),
        (
            "scatter: Items\n  keep if: {{_}}",
            ['["a","b"]', "true", "false"],
            ["Text[]", "Boolean", "Boolean"],
            "Text[]",
            '["a"]',
        ),
        (
            "scatter: Items\n  drop if: {{_}}",
            ['["a","b"]', "true", "false"],
            ["Text[]", "Boolean", "Boolean"],
            "Text[]",
            '["b"]',
        ),
        (
            "scatter: Items\n  sort ascending by: {{_}}",
            ['["a","b"]', "2", "1"],
            ["Text[]", "Number", "Number"],
            "Text[]",
            '["b","a"]',
        ),
        (
            "scatter: Items\n  gather using: {{_}}",
            ['["a","b"]', "joined"],
            ["Text[]", "Text"],
            "Text",
            "joined",
        ),
        (
            "scatter: Items\n  settle using: {{_}} {{_1._}}",
            ['["a","b"]', "joined"],
            ["Text[]", "Text"],
            "Text",
            "joined",
        ),
        (
            "repeat 2 times:\n    run: {{_}}\n    until: Done?",
            ["done", "true"],
            ["Text", "Boolean"],
            "Text",
            "done",
        ),
    ],
    ids=[
        "run",
        "scatter",
        "storm",
        "map",
        "keep",
        "drop",
        "sort",
        "gather",
        "settle",
        "until",
    ],
)
def test_adhoc_defaults_reach_model_schemas_and_persisted_outputs(
    tmp_path: Path,
    body: str,
    responses: list[str],
    child_types: list[str],
    result_type: str,
    result: str,
) -> None:
    annotation = "" if result_type == "Text" else f" -> {result_type}"
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow main{annotation}:
  recall = none
  instruct = none
  context = none
  lanes = 1
  {body}
""",
        responses=[
            ModelCallResult(message=Message.assistant(text)) for text in responses
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="main", primary=(TextPart("seed"),)
                )
            )
            assert root.status == "succeeded", root.error
            assert root.output is not None and root.output.local.type == result_type
            assert harness.store.run_output_text(run_id=root.id) == result
            children = [
                run
                for run in harness.store.list_run_tree(root_run_id=root.id)
                if run.parent
            ]
            assert all(
                run.status == "succeeded" and run.output is not None for run in children
            )
            assert sorted(
                run.output.local.type for run in children if run.output
            ) == sorted(child_types)
            schemas = {
                "Text": None,
                "Text[]": {"type": "array", "items": {"type": "string"}},
                "Boolean": {"type": "boolean"},
                "Number": {"type": "number"},
            }
            assert [
                call.call.output_schema for call in harness.adapter.invocations
            ] == [schemas[type_name] for type_name in child_types]
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "header,input_type,output_type,items",
    [
        ("run worker", "Number", "Text", None),
        ("scatter using worker", "Number", "Text[]", None),
        ("storm 2 using worker", "Number", "Text", None),
        ("map using worker", "Number", "Text", '["1","invalid"]'),
        ("keep if worker", "Number", "Boolean", '["1","invalid"]'),
        ("drop if worker", "Number", "Boolean", '["1","invalid"]'),
        ("sort ascending by worker", "Number", "Number", '["1","invalid"]'),
        ("gather using worker", "Number[]", "Text", '["1","invalid"]'),
        ("gather using worker", "Number", "Text", '["1","2"]'),
        ("settle using worker", "Number", "Text", '["seed","1","invalid"]'),
    ],
    ids=[
        "run",
        "scatter",
        "storm",
        "map",
        "keep",
        "drop",
        "sort",
        "gather-elements",
        "gather-scalar",
        "settle",
    ],
)
def test_incompatible_call_inputs_fail_before_any_target_model_call(
    tmp_path: Path, header: str, input_type: str, output_type: str, items: str | None
) -> None:
    prefix = "scatter: Items\n  " if items is not None else ""
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
agic worker(_: {input_type}) -> {output_type}:
  Work with {{{{_}}}}.
flow main:
  recall = none
  instruct = none
  context = none
  {prefix}{header}
""",
        responses=[ModelCallResult(message=Message.assistant(items))]
        if items is not None
        else [],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="main", primary=(TextPart("invalid"),)
                )
            )
            assert root.status == "failed"
            assert root.error is not None
            error = harness.store.resolve_error(root.error)
            if header == "gather using worker" and input_type == "Number":
                assert error == "Part[] can only contain Part values"
            else:
                assert "Number" in error
            assert len(harness.adapter.invocations) == (1 if items is not None else 0)
            assert harness.adapter.pending_responses == 0
            children = [
                run
                for run in harness.store.list_run_tree(root_run_id=root.id)
                if run.parent
            ]
            assert len(children) == (1 if items is not None else 0)
            failed_step = harness.store.list_steps(run_id=root.id)[-1]
            assert failed_step.status == "failed"
            assert failed_step.output is None

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", ["2", "invalid"])
def test_empty_map_skips_element_conversion_but_checks_named_arguments(
    tmp_path: Path, limit: str
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker(_: Number, limit: Number) -> Number:
  {{_}} {{limit}}
flow main(limit) -> Number[]:
  recall = none
  instruct = none
  context = none
  scatter: Items
  map using worker
""",
        responses=[ModelCallResult(message=Message.assistant("[]"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="main", named={"limit": limit})
            )
            if limit == "2":
                assert root.status == "succeeded", root.error
                assert root.output is not None and root.output.local.type == "Number[]"
                assert harness.store.run_output_text(run_id=root.id) == "[]"
            else:
                assert root.status == "failed" and root.error is not None
                assert "Number" in harness.store.resolve_error(root.error)
            assert len(harness.adapter.invocations) == 1
            assert harness.adapter.pending_responses == 0
            assert len(harness.store.list_run_tree(root_run_id=root.id)) == 2

    asyncio.run(scenario())
