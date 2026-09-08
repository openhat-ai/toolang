"""Human compaction shares durable execution without model/budget triggering."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path

import pytest
from toolang.base.errors import ToolangError

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, TextPart
from toolang.base.types.model import ModelOverride
from toolang.base.types.run import ModelCallResult
from toolang.execution.client import LocalRunClient
from toolang.execution.events import RunEnd
from toolang.execution.executor.compact import available_horizon, permit
from toolang.execution.history import RunHistory
from toolang.execution.records import RunControlPayload
from toolang.execution.schemas import CompactRequest
from toolang.execution.store import RunStore
from toolang.execution.types import RunCommand, RunRef, ThreadPrefix
from toolang.plugin.models.collections import ModelCollection
from toolang.lang.types import Struct


SOURCE = """agic chat(_: Part[]) -> Text:
  context: none
  instruct: none
  user: {{_}}
"""


def reply(value):
    return ModelCallResult(message=Message.assistant(json.dumps(value)))


def harness_for(root):
    harness = ExecutionHarness.create(root, source=SOURCE, responses=[reply("ACK")] * 3)
    harness.setup = replace(
        harness.setup,
        models=ModelCollection(
            tuple(
                replace(
                    entry,
                    target=replace(entry.target, structured_output=True),
                    info=replace(
                        entry.info,
                        metadata={
                            "reasoning_options": [
                                {"type": "effort", "values": ["low", "high"]}
                            ]
                        },
                    ),
                )
                for entry in harness.setup.models.entries
            )
        ),
    )
    harness.executor._setup = lambda: harness.setup
    return harness


async def seed(harness):
    thread = harness.threads.create(prefix=ThreadPrefix.TERM)
    runs = [
        await harness.executor.run(
            harness.run_spec(
                thread=thread,
                runnable="chat",
                primary=(TextPart(f"fact {i}"),),
            )
        )
        for i in range(3)
    ]
    assert all(run.status == "succeeded" for run in runs)
    return thread, runs


def compact_replies(thread, end, *, begin=None, gate=None):
    return [
        ScriptedModelTurn(
            reply({"summary": "", "position": None, "complete": False}), gate=gate
        ),
        reply({"summary": "Earlier facts", "position": None, "complete": True}),
        ModelCallResult(message=Message.assistant("true")),
        reply(
            {"thread": thread, "begin": begin, "end": end, "summary": "Earlier facts"}
        ),
    ]


@pytest.mark.parametrize("explicit", [False, True])
def test_manual_compact_and_next_root_adoption(tmp_path: Path, explicit: bool):
    h = harness_for(tmp_path)

    async def scenario():
        async with h:
            thread, runs = await seed(h)
            end = RunRef(runs[1 if explicit else 2].id)
            before = [h.store.list_steps(run_id=run.id) for run in runs]
            h.adapter._responses.extend(compact_replies(thread, str(end)))
            client = LocalRunClient(h.executor)
            await client.connect()
            handle = await client.compact(
                CompactRequest(
                    {"thread": thread, **({"end": str(end)} if explicit else {})},
                    "manual",
                    model=ModelOverride(h.setup.defaults.model.ref, effort="low"),
                    commands=(RunCommand("limit", "time", 60),),
                )
            )
            result = await handle.wait()
            assert result.status == "succeeded", result.error
            assert result.thread_id == f"compact_{thread}"
            assert result.output is not None
            assert isinstance(result.output.local.value, Struct)
            assert dict(result.output.local.value) == {
                "thread": thread,
                "begin": None,
                "end": str(end),
                "summary": "Earlier facts",
            }
            payload = h.store.list_run_controls(run_id=result.id)[0].payload
            assert isinstance(payload, RunControlPayload)
            assert payload.limits.time == 60
            assert payload.model_request.parameters.reasoning.effort == "low"
            history = RunHistory(h.store)
            compaction = history.get_compaction(thread)
            assert compaction is not None
            assert available_horizon(h.store, thread) == compaction.ref
            assert [h.store.list_steps(run_id=run.id) for run in runs] == before
            assert len(history.thread_view(thread).roots) == 3
            assert all(
                c.kind != "compact"
                for r in runs
                for c in h.store.list_run_controls(run_id=r.id)
            )
            h.adapter._responses.append(reply("new"))
            latest = await h.executor.run(
                h.run_spec(thread=thread, runnable="chat", primary=(TextPart("now"),))
            )
            assert (
                h.store.list_run_controls(run_id=latest.id)[0].payload.horizon
                == compaction.ref
            )
            call = h.adapter.invocations[-1].call
            assert not any(tool.name == "_toolang__compact" for tool in call.tools)
            text = "\n".join(
                p.text
                for m in call.messages
                for p in m.parts
                if isinstance(p, TextPart)
            )
            assert "Earlier facts" in text and "fact 0" not in text and "fact 2" in text
            model = next(
                s for s in h.store.list_steps(run_id=latest.id) if s.kind == "model"
            )
            assert history.get_model_call(model.ref) == call
            await client.disconnect()

    asyncio.run(scenario())


def test_invalid_output_fails_without_shadowing_previous_horizon(tmp_path: Path):
    h = harness_for(tmp_path)
    tracer = RecordingRunTracer()

    async def scenario():
        async with h:
            thread, runs = await seed(h)
            h.adapter._responses.extend(compact_replies(thread, runs[-1].id))
            first = await h.executor.compact(
                CompactRequest({"thread": thread}, "first")
            )
            original = available_horizon(h.store, thread)
            assert first.status == "succeeded" and original is not None
            h.adapter._responses.extend(compact_replies(thread, runs[-1].id, begin=""))
            second = await h.executor.compact(
                CompactRequest({"thread": thread, "begin": runs[0].id}, "second"),
                tracer=tracer,
            )
            assert second.id != first.id and second.status == "failed"
            assert "match its coverage" in second.error.message
            assert available_horizon(h.store, thread) == original
            assert isinstance(tracer.events[-1], RunEnd)
            assert tracer.events[-1].status == "failed"

    asyncio.run(scenario())


def test_manual_compact_leaves_active_root_binding_unchanged(tmp_path: Path):
    h = harness_for(tmp_path)
    gate = AsyncGate()

    async def scenario():
        async with h:
            thread, runs = await seed(h)
            h.adapter._responses.append(ScriptedModelTurn(reply("active"), gate=gate))
            active = h.executor.run(
                h.run_spec(thread=thread, runnable="chat", primary=(TextPart("now"),))
            )
            await gate.wait_until_entered()
            with pytest.raises(ToolangError, match="retain a terminal root"):
                h.executor.compact(
                    CompactRequest(
                        {"thread": thread, "end": active.run_id}, "active-boundary"
                    )
                )
            before = h.store.list_run_controls(run_id=active.run_id)
            h.adapter._responses.extend(compact_replies(thread, runs[-1].id))
            compact = await h.executor.compact(
                CompactRequest({"thread": thread}, "manual")
            )
            assert compact.status == "succeeded", compact.error
            assert h.store.list_run_controls(run_id=active.run_id) == before
            assert before[0].payload.horizon is None
            assert available_horizon(h.store, thread) is not None
            gate.release()
            assert (await active).status == "succeeded"

    asyncio.run(scenario())


def test_append_while_waiting_preserves_requested_boundary(tmp_path: Path):
    h = harness_for(tmp_path)

    async def scenario():
        async with h:
            thread, runs = await seed(h)
            lock = h.store.db_path.with_name(
                f"{h.store.db_path.name}.{thread}.compact.lock"
            )
            async with permit(lock):
                handle = h.executor.compact(
                    CompactRequest({"thread": thread}, "waiting")
                )
                h.adapter._responses.append(reply("appended"))
                appended = await h.executor.run(
                    h.run_spec(
                        thread=thread, runnable="chat", primary=(TextPart("new"),)
                    )
                )
                assert appended.status == "succeeded"
                h.adapter._responses.extend(compact_replies(thread, runs[-1].id))
            result = await handle
            assert result.status == "succeeded", result.error
            compact = RunHistory(h.store).get_compaction(thread)
            assert compact is not None
            assert isinstance(compact.output.local.value, Struct)
            assert compact.output.local.value["end"] == runs[-1].id

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["first", "unknown", "compact", "model"])
def test_reject_before_starting_compact(tmp_path: Path, invalid: str):
    h = harness_for(tmp_path)

    async def scenario():
        async with h:
            thread, runs = await seed(h)
            kwargs = {}
            input = {}
            if invalid == "first":
                input["end"] = runs[0].id
            if invalid == "unknown":
                input["end"] = "run_unknown"
            if invalid == "model":
                kwargs["model"] = ModelOverride("outside/model")
            target = f"compact_{thread}" if invalid == "compact" else thread
            with pytest.raises(ToolangError):
                h.executor.compact(
                    CompactRequest({"thread": target, **input}, "invalid", **kwargs)
                )
            assert len(h.adapter.invocations) == 3
            assert h.store.get_thread(thread_id=f"compact_{thread}") is None

    asyncio.run(scenario())


def test_manual_compactions_serialize_and_cancel_waiter_only(tmp_path: Path):
    h = harness_for(tmp_path)
    gate = AsyncGate()

    async def scenario():
        async with h:
            thread, runs = await seed(h)
            h.adapter._responses.extend(compact_replies(thread, runs[-1].id, gate=gate))
            first = h.executor.compact(CompactRequest({"thread": thread}, "first"))
            await gate.wait_until_entered()
            second = h.executor.compact(CompactRequest({"thread": thread}, "second"))
            await asyncio.sleep(0.02)
            assert len(h.adapter.invocations) == 4
            second.cancel()
            assert (await second).status == "canceled"
            assert not first.task.done()
            gate.release()
            assert (await first).status == "succeeded"
            h.adapter._responses.extend(compact_replies(thread, runs[-1].id))
            assert (
                await h.executor.compact(
                    CompactRequest({"thread": thread, "begin": runs[0].id}, "third")
                )
            ).status == "succeeded"

    asyncio.run(scenario())


def test_rewind_while_waiting_invalidates_frozen_range(tmp_path: Path):
    h = harness_for(tmp_path)

    async def scenario():
        async with h:
            thread, runs = await seed(h)
            lock = h.store.db_path.with_name(
                f"{h.store.db_path.name}.{thread}.compact.lock"
            )
            async with permit(lock):
                handle = h.executor.compact(
                    CompactRequest({"thread": thread}, "waiting")
                )
                await asyncio.sleep(0)
                h.threads.rewind(thread_id=thread, run_id=runs[-1].id)
            result = await handle
            assert result.status == "failed"
            assert "range changed" in result.error.message
            assert len(h.adapter.invocations) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "begin,bare,end,expected_begin,reuse",
    [
        (None, False, 5, 2, True),
        (None, True, 5, 2, False),
        (2, False, 5, 2, True),
        (2, True, 5, 2, False),
        (0, False, 5, None, False),
        (0, True, 5, None, False),
        (1, False, 5, 1, False),
        (3, False, 5, 3, False),
        (None, False, 1, None, False),
        (0, False, 2, None, False),
    ],
)
def test_incremental_and_independent_ranges(
    tmp_path, begin, bare, end, expected_begin, reuse
):
    h = harness_for(tmp_path)

    async def scenario():
        async with h:
            thread, runs = await seed(h)
            h.adapter._responses.extend(compact_replies(thread, runs[2].id))
            first = await h.executor.compact(
                CompactRequest({"thread": thread}, "first")
            )
            assert first.status == "succeeded", first.error
            original = available_horizon(h.store, thread)
            assert original is not None
            for i in range(3):
                h.adapter._responses.append(reply("ACK"))
                runs.append(
                    await h.executor.run(
                        h.run_spec(
                            thread=thread,
                            runnable="chat",
                            primary=(TextPart(f"more {i}"),),
                        )
                    )
                )
            input = {"thread": thread, "end": runs[end].id, "bare": str(bare).lower()}
            if begin is not None:
                input["begin"] = runs[begin].id
            start = runs[expected_begin].id if expected_begin is not None else None
            coverage = None if reuse else start
            h.adapter._responses.extend(
                compact_replies(thread, runs[end].id, begin=coverage)
            )
            second = await h.executor.compact(CompactRequest(input, "second"))
            assert second.status == "succeeded", h.store.resolve_error(second.error)
            payload = h.store.list_run_controls(run_id=second.id)[0].payload
            assert payload.input["thread"] == thread
            assert payload.input.get("begin") == start
            assert payload.input["end"] == runs[end].id
            assert payload.input.get("previous") == (str(original) if reuse else None)
            assert dict(payload.authored_input) == input
            selected = available_horizon(h.store, thread)
            latest = RunHistory(h.store).get_compaction(thread)
            assert latest is not None
            assert selected == (latest.ref if coverage is None else original)
            # A following root freezes exactly the applicable selection, not the latest test.
            h.adapter._responses.append(reply("new"))
            new = await h.executor.run(
                h.run_spec(thread=thread, runnable="chat", primary=(TextPart("now"),))
            )
            assert (
                h.store.list_run_controls(run_id=new.id)[0].payload.horizon == selected
            )
            call = h.adapter.invocations[-1].call
            model = next(
                s for s in h.store.list_steps(run_id=new.id) if s.kind == "model"
            )
            assert RunHistory(h.store).get_model_call(model.ref) == call
            reopened = RunStore(h.store.db_path, read_only=True)
            try:
                assert available_horizon(reopened, thread) == selected
                assert RunHistory(reopened).get_model_call(model.ref) == call
            finally:
                reopened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("input_kind", ["equal", "reversed", "unknown", "child"])
def test_invalid_begin_creates_no_compact_run(tmp_path, input_kind):
    h = harness_for(tmp_path)

    async def scenario():
        async with h:
            thread, runs = await seed(h)
            h.adapter._responses.extend(compact_replies(thread, runs[2].id))
            first = await h.executor.compact(
                CompactRequest({"thread": thread}, "first")
            )
            input = {"thread": thread}
            if input_kind == "reversed":
                input.update(begin=runs[2].id, end=runs[1].id)
            elif input_kind == "unknown":
                input["begin"] = "run_unknown"
            elif input_kind == "child":
                child = next(
                    r
                    for r in RunHistory(h.store)
                    .thread_view(f"compact_{thread}")
                    .members
                    if r.id != first.id
                )
                input["begin"] = child.id
            before = len(h.adapter.invocations)
            with pytest.raises(ToolangError):
                h.executor.compact(CompactRequest(input, "invalid"))
            assert len(h.adapter.invocations) == before
            assert len(RunHistory(h.store).thread_view(f"compact_{thread}").roots) == 1

    asyncio.run(scenario())


def test_previous_selection_is_frozen_before_waiting(tmp_path):
    h = harness_for(tmp_path)
    gate = AsyncGate()

    async def scenario():
        async with h:
            thread, runs = await seed(h)
            h.adapter._responses.extend(compact_replies(thread, runs[1].id))
            first = await h.executor.compact(
                CompactRequest({"thread": thread, "end": runs[1].id}, "first")
            )
            assert first.status == "succeeded"
            original = available_horizon(h.store, thread)
            h.adapter._responses.extend(compact_replies(thread, runs[2].id, gate=gate))
            second = h.executor.compact(CompactRequest({"thread": thread}, "second"))
            await gate.wait_until_entered()
            third = h.executor.compact(CompactRequest({"thread": thread}, "third"))
            gate.release()
            assert (await second).status == "succeeded"
            h.adapter._responses.extend(compact_replies(thread, runs[2].id))
            result = await third
            assert result.status == "succeeded", result.error
            assert h.store.list_run_controls(run_id=result.id)[0].payload.input[
                "previous"
            ] == str(original)

    asyncio.run(scenario())


@pytest.mark.parametrize("reuse", [False, True])
def test_compact_rejects_misrepresented_coverage(tmp_path, reuse):
    h = harness_for(tmp_path)

    async def scenario():
        async with h:
            thread, runs = await seed(h)
            if reuse:
                h.adapter._responses.extend(compact_replies(thread, runs[1].id))
                assert (
                    await h.executor.compact(
                        CompactRequest({"thread": thread, "end": runs[1].id}, "first")
                    )
                ).status == "succeeded"
            original = available_horizon(h.store, thread)
            # An interval cannot claim a prefix; a cumulative output must include its prefix.
            h.adapter._responses.extend(
                compact_replies(thread, runs[2].id, begin=runs[1].id if reuse else None)
            )
            result = await h.executor.compact(
                CompactRequest(
                    {"thread": thread, "begin": runs[1].id}, "wrong-coverage"
                )
            )
            assert result.status == "failed"
            assert available_horizon(h.store, thread) == original

    asyncio.run(scenario())
