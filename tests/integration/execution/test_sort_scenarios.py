"""Directional sorting and its independent positional-selection boundary."""

import asyncio
from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.execution.executor.common import Local
from toolang.execution.executor.stmts import filter as filter_stmt
from toolang.execution.schemas import StepData
from toolang.execution.store import RunStore
from toolang.execution.types import ThreadPrefix, TypedRef
from toolang.lang.ast import SortStmt
from toolang.lang.input import resolve_input_parts
from toolang.lang.types import Array


DECLARATIONS = """agic split(_: Text) -> Text[]:
  recall = none
  {{_}}

agic score(_: Text) -> Number:
  recall = none
  {{_}}

flow work(_: Text) -> Text[]:
  scatter 3 using split
"""


def _response(text: str) -> ModelCallResult:
    return ModelCallResult(message=Message.assistant(text))


@pytest.mark.parametrize(
    ("order", "selection", "expected"),
    [
        ("ascending", "", ["c", "a", "b"]),
        ("descending", "", ["a", "b", "c"]),
        ("descending", "keep first 2", ["a", "b"]),
        ("descending", "keep last 2", ["b", "c"]),
        ("ascending", "keep first 0", []),
        ("descending", "keep last 0", []),
        ("ascending", "keep first 8", ["c", "a", "b"]),
        ("descending", "keep last 8", ["a", "b", "c"]),
        ("ascending", "let best = keep first 2", ["c", "a", "b"]),
        ("ascending", "let keep first 2", ["c", "a", "b"]),
    ],
)
def test_sort_and_selection_commit_typed_results_without_extra_model_calls(
    tmp_path: Path, order: str, selection: str, expected: list[str]
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=DECLARATIONS
        + f"  sort {order} in 2 lanes by score\n"
        + (f"  {selection}\n" if selection else ""),
        responses=[_response('["a","b","c"]'), *map(_response, ("2", "2", "-1"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="work", primary=resolve_input_parts("items")
                )
            )
            assert root.status == "succeeded", root.error
            assert json.loads(harness.store.run_output_text(run_id=root.id)) == expected
            assert len(harness.adapter.invocations) == 4
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == (
                ["run", "par", "value"] if selection else ["run", "par"]
            )
            sort = steps[1]
            assert isinstance(sort.given, SortStmt)
            assert sort.given.order == order
            assert sort.output is not None
            assert (sort.output.type, sort.output.dim) == ("Text[]", 1)
            assert isinstance(sort.output.value, Array)
            source_output = steps[0].output
            assert source_output is not None
            assert isinstance(source_output.value, TypedRef)
            source_ref = source_output.value.ref
            assert tuple(sort.output.value) == tuple(
                TypedRef(source_ref.select(index), "Text")
                for index in ([2, 0, 1] if order == "ascending" else [0, 1, 2])
            )
            if selection:
                selected = steps[2]
                assert selected.output is not None
                assert (selected.output.type, selected.output.dim) == ("Text[]", 1)
                selected_value = harness.store.resolve_value(selected.output.value)
                assert isinstance(selected_value, Array)
                assert list(selected_value) == (
                    ["c", "a"] if selection.startswith("let ") else expected
                )
                assert selected.output.name == (
                    "best"
                    if selection.startswith("let best")
                    else None
                    if selection.startswith("let ")
                    else "_"
                )
            reopened = RunStore(harness.store.db_path, read_only=True)
            try:
                assert reopened.list_steps(run_id=root.id)[1] == sort
            finally:
                reopened.close()
            adapter = TypeAdapter(StepData)
            public = StepData.from_record(sort)
            assert (
                adapter.validate_python(adapter.dump_python(public, mode="json"))
                == public
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("binding", ["let sorted_items = ", "let "])
def test_sort_named_and_discarded_bindings_preserve_primary_collection(
    tmp_path: Path, binding: str
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=DECLARATIONS + f"  {binding}sort ascending by score\n",
        responses=[_response('["a","b","c"]'), *map(_response, ("2", "2", "-1"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="work", primary=resolve_input_parts("items")
                )
            )
            assert root.status == "succeeded", root.error
            assert json.loads(harness.store.run_output_text(run_id=root.id)) == [
                "a",
                "b",
                "c",
            ]

    asyncio.run(scenario())


def test_empty_sort_does_not_invoke_a_scorer(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=DECLARATIONS + "  sort ascending by score\n  keep first 3\n",
        responses=[_response("[]")],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="work", primary=resolve_input_parts("items")
                )
            )
            assert root.status == "succeeded", root.error
            assert json.loads(harness.store.run_output_text(run_id=root.id)) == []
            assert len(harness.adapter.invocations) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("order", ["ascending", "descending"])
def test_sort_preserves_integer_score_precision(tmp_path: Path, order: str) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=DECLARATIONS + f"  sort {order} in 1 lane by score\n",
        responses=[
            _response('["a","b","c"]'),
            *map(
                _response, ("9007199254740993", "9007199254740992", "9007199254740994")
            ),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="work", primary=resolve_input_parts("items")
                )
            )
            assert root.status == "succeeded", root.error
            assert json.loads(harness.store.run_output_text(run_id=root.id)) == (
                ["b", "a", "c"] if order == "ascending" else ["c", "a", "b"]
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("canceled", [False, True])
def test_retry_after_sort_preserves_committed_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, canceled: bool
) -> None:
    require_list = filter_stmt.require_list
    calls = 0

    def interrupted_selection(
        locals: Mapping[str, Local], *, operation: str
    ) -> list[Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            if canceled:
                raise asyncio.CancelledError()
            raise RuntimeError("selection interrupted")
        return require_list(locals, operation=operation)

    monkeypatch.setattr(filter_stmt, "require_list", interrupted_selection)
    harness = ExecutionHarness.create(
        tmp_path,
        source=DECLARATIONS + "  sort descending by score\n  keep first 2\n",
        responses=[_response('["a","b","c"]'), *map(_response, ("2", "2", "-1"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="work", primary=resolve_input_parts("items")
                )
            )
            assert root.status == ("canceled" if canceled else "failed")
            assert json.loads(harness.store.run_output_text(run_id=root.id)) == [
                "a",
                "b",
                "c",
            ]
            committed = harness.store.list_steps(run_id=root.id)[1]
            retried = await harness.executor.retry(
                root.id, setup=harness.setup, state=harness.state
            )
            assert retried.status == "succeeded", retried.error
            assert json.loads(harness.store.run_output_text(run_id=root.id)) == [
                "a",
                "b",
            ]
            assert harness.store.list_steps(run_id=root.id)[1] == committed
            assert len(harness.adapter.invocations) == 4

    asyncio.run(scenario())


def test_sort_lanes_and_ties_ignore_completion_order(tmp_path: Path) -> None:
    gates = [AsyncGate() for _ in range(3)]
    harness = ExecutionHarness.create(
        tmp_path,
        source=DECLARATIONS + "  sort descending in 2 lanes by score\n",
        responses=[_response('["a","b","c"]')]
        + [ScriptedModelTurn(result=_response("2"), gate=gate) for gate in gates],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="work", primary=resolve_input_parts("items")
                )
            )
            await asyncio.wait_for(
                asyncio.gather(
                    gates[0].wait_until_entered(), gates[1].wait_until_entered()
                ),
                timeout=2,
            )
            assert not gates[2].entered
            gates[1].release()
            await asyncio.wait_for(gates[2].wait_until_entered(), timeout=2)
            gates[2].release()
            gates[0].release()
            root = await asyncio.wait_for(handle, timeout=3)
            assert root.status == "succeeded", root.error
            assert json.loads(harness.store.run_output_text(run_id=root.id)) == [
                "a",
                "b",
                "c",
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize("result", ["not a number", "true"])
def test_invalid_score_fails_before_sort_binding(tmp_path: Path, result: str) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=DECLARATIONS + "  sort ascending in 1 lane by score\n  keep first 1\n",
        responses=[_response('["a","b","c"]')] + [_response(result)] * 20,
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="work", primary=resolve_input_parts("items")
                )
            )
            assert root.status == "failed"
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == ["run", "par"]
            assert steps[1].output is None
            assert steps[0].output is not None
            resolved = harness.store.resolve_value(steps[0].output.value)
            assert isinstance(resolved, Array)
            assert list(resolved) == ["a", "b", "c"]
            assert harness.adapter.pending_responses > 0

    asyncio.run(scenario())
