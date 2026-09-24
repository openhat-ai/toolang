"""Empty, singleton, and multi-item collections retain their operation contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.execution.types import ThreadPrefix


def _check(
    root: Path,
    *,
    body: str,
    responses: list[str],
    output_type: str,
    expected: str | None,
    error: str | None = None,
) -> None:
    harness = ExecutionHarness.create(
        root,
        source=f"""
flow main() -> {output_type}:
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
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="main")
            )
            assert len(harness.adapter.invocations) == len(responses)
            assert harness.adapter.pending_responses == 0
            children = [
                child
                for child in harness.store.list_run_tree(root_run_id=run.id)
                if child.parent
            ]
            assert len(children) == len(responses)
            assert all(child.status == "succeeded" for child in children)
            step = harness.store.list_steps(run_id=run.id)[-1]
            if error is not None:
                assert run.status == "failed"
                assert run.error is not None
                assert harness.store.resolve_error(run.error) == error
                assert step.status == "failed" and step.output is None
            else:
                assert run.status == "succeeded", run.error
                assert run.output is not None and run.output.local.type == output_type
                assert step.output is not None and step.output.local.type == output_type
                assert harness.store.run_output_text(run_id=run.id) == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [0, 1, 3], ids=["empty", "singleton", "multiple"])
@pytest.mark.parametrize(
    "operation,answers,expected,output_type",
    [
        (
            "map using -> Number[]",
            ["[1,10]", "[2,20]", "[3,30]"],
            ["[]", "[[1,10]]", "[[1,10],[2,20],[3,30]]"],
            "Number[][]",
        ),
        ("keep if", ["false", "true", "true"], ["[]", "[]", '["b","c"]'], "Text[]"),
        ("drop if", ["true", "false", "false"], ["[]", "[]", '["b","c"]'], "Text[]"),
        (
            "sort ascending by",
            ["3", "1", "2"],
            ["[]", '["a"]', '["b","c","a"]'],
            "Text[]",
        ),
        ("keep first 2", [], ["[]", '["a"]', '["a","b"]'], "Text[]"),
        ("drop last 2", [], ["[]", "[]", '["a"]'], "Text[]"),
    ],
    ids=[
        "map-array",
        "keep-predicate",
        "drop-predicate",
        "sort",
        "keep-position",
        "drop-position",
    ],
)
def test_collection_size_preserves_types_order_and_call_count(
    tmp_path: Path,
    size: int,
    operation: str,
    answers: list[str],
    expected: list[str],
    output_type: str,
) -> None:
    source = json.dumps(["a", "b", "c"][:size])
    statement = f"{operation}: {{{{_}}}}" if answers else operation
    _check(
        tmp_path,
        body=f"scatter: Items\n  {statement}",
        responses=[source, *answers[:size]],
        output_type=output_type,
        expected=expected[[0, 1, 3].index(size)],
    )


@pytest.mark.parametrize("size", [0, 1, 3], ids=["empty", "singleton", "multiple"])
@pytest.mark.parametrize("operation", ["gather", "settle", "settle-from"])
def test_reduction_size_controls_seed_and_child_call_count(
    tmp_path: Path, size: int, operation: str
) -> None:
    items = ["a", "b", "c"][:size]
    if operation == "gather":
        statement = "gather using: {{_}}"
        answers = ["joined"] if size else []
    else:
        statement = "settle:\n    {{_}} {{_1._}}"
        if operation == "settle-from":
            statement += "\n    from: seed"
        calls = size if operation == "settle-from" else max(0, size - 1)
        answers = [f"merged-{index}" for index in range(1, calls + 1)]
    expected = answers[-1] if answers else "a" if size else None
    _check(
        tmp_path,
        body=f"scatter: Items\n  {statement}",
        responses=[json.dumps(items), *answers],
        output_type="Text",
        expected=expected,
        error=f"{operation.split('-')[0]} requires a nonempty list"
        if not size
        else None,
    )


@pytest.mark.parametrize("size", [0, 1, 3], ids=["empty", "singleton", "multiple"])
@pytest.mark.parametrize("operation", ["scatter", "storm"])
def test_producer_size_preserves_empty_and_nested_array_results(
    tmp_path: Path, size: int, operation: str
) -> None:
    items = ["a", "b", "c"][:size]
    if operation == "scatter":
        body = "scatter: Items"
        responses = [json.dumps(items)]
        expected = json.dumps(items, separators=(",", ":"))
        output_type = "Text[]"
    else:
        body = f"storm {size} using -> Text[]: Items"
        responses = [json.dumps([item]) for item in items]
        expected = json.dumps([[item] for item in items], separators=(",", ":"))
        output_type = "Text[][]"
    _check(
        tmp_path,
        body=body,
        responses=responses,
        output_type=output_type,
        expected=expected,
    )
