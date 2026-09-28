"""Durable batch checkpoints and publication survive process interruption."""

import asyncio
from contextlib import closing
from dataclasses import replace

import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.errors import ModelResponseError, ToolangError
from toolang.base.types.message import Message, TextPart
from toolang.base.types.model import ModelRequest
from toolang.base.types.run import ModelCallResult, ModelUsage
from toolang.execution import batched_compaction as reducer
from toolang.execution.executor.compact import CompactSpec, produce
from toolang.execution.inspection.history import RunHistory
from toolang.execution.records import StoredModelStepGiven
from toolang.execution.store import RunStore
from toolang.execution.types import (
    ModelStepNoted,
    RunRef,
    ThreadPrefix,
    ThreadRef,
    ToolStepGiven,
)

SOURCE = """agic chat(_: Part[]) -> Text:
  context = none
  instruct = none
  user: {{_}}
"""


def reply(text="cumulative summary"):
    return ModelCallResult(
        message=Message.assistant(text),
        usage=ModelUsage(input_tokens=42, output_tokens=5),
    )


@pytest.fixture(autouse=True)
def offline_estimate(monkeypatch):
    # Admit exactly one complete exchange per batch, independent of BPE assets.
    monkeypatch.setattr(
        reducer,
        "estimate_model_input_tokens",
        lambda call, model: (
            100
            + 4000
            * sum(
                isinstance(part, TextPart)
                and part.text.startswith("[Begin historical Run")
                for message in call.messages
                for part in message.parts
            )
        ),
    )


async def seed(harness):
    thread = harness.threads.create(prefix=ThreadPrefix.TERM)
    roots = []
    for index in range(4):
        root = await harness.executor.run(
            harness.run_spec(
                thread=thread, runnable="chat", primary=(TextPart(f"input {index}"),)
            )
        )
        assert root.status == "succeeded"
        roots.append(RunRef(root.id))
    model = replace(
        harness.setup.models_effective()[0], limit={"context": 10000, "output": 512}
    )
    return CompactSpec(
        target=ThreadRef.parse(thread),
        roots=tuple(roots),
        begin=roots[0],
        end=roots[-1],
        summary="",
        prior=None,
        model=model,
        request=ModelRequest(model.ref),
        adapter=harness.adapter,
        environ={},
        setup=harness.setup.revision,
        state=harness.state.revision,
        sandbox="host",
        limits=harness.setup.limits,
        size=100,
        versions=harness.store.history_versions([str(root) for root in roots]),
    )


def harness_at(path):
    return ExecutionHarness.create(
        path, source=SOURCE, responses=[reply(f"output {i}") for i in range(4)]
    )


def test_batches_are_reconstructable_without_accumulating_previous_requests(tmp_path):
    harness = harness_at(tmp_path)

    async def scenario():
        async with harness:
            spec = await seed(harness)
            harness.adapter._responses.extend([reply(f"summary {i}") for i in range(3)])
            output = await produce(
                harness.store, spec, issue_run=harness.executor.ids.issue_run
            )
            with closing(RunStore(harness.store.db_path)) as store:
                history = RunHistory(store)
                steps = store.list_steps(run_id=str(output.ref))
                assert [step.kind for step in steps] == ["tool", "model"] * 3
                assert history.get_compaction(spec.target) == output
                assert output.result.end == str(spec.end)
                assert output.result.summary == "summary 2"
                for index in range(3):
                    read, model = steps[index * 2 : index * 2 + 2]
                    assert (
                        isinstance(read.given, ToolStepGiven)
                        and read.given.trigger == "runtime"
                    )
                    assert read.given.call.input["roots"] == [str(spec.roots[index])]
                    assert isinstance(model.given, StoredModelStepGiven)
                    assert model.given.call.messages.head == model.ref
                    call = history.get_model_call(model.ref)
                    assert call == harness.adapter.invocations[4 + index].call
                    assert not call.tools
                    assert f"input {index}" in str(call.messages)
                    if index:
                        assert f"summary {index - 1}" in str(call.messages)
                        assert f"input {index - 1}" not in str(call.messages)
                    assert "input 3" not in str(call.messages)
                    assert isinstance(model.noted, ModelStepNoted)
                    assert model.noted.accounting is not None
                    assert model.noted.accounting.input_tokens == 42
                # Retrying an already published request is a no-op.
                assert (
                    await produce(store, spec, issue_run=harness.executor.ids.issue_run)
                    == output
                )
                assert len(harness.adapter.invocations) == 7

    asyncio.run(scenario())


class ProcessCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "method,kind,after",
    [
        ("accept_run", None, True),
        ("begin_run", None, False),
        ("begin_run", None, True),
        ("begin_step", "tool", False),
        ("begin_step", "tool", True),
        ("finish_step", "tool", False),
        ("finish_step", "tool", True),
        ("begin_step", "model", False),
        ("begin_step", "model", True),
        ("finish_step", "model", False),
        ("finish_step", "model", True),
        ("finish_run", None, False),
        ("finish_run", None, True),
        ("publish_compaction", None, False),
        ("publish_compaction", None, True),
    ],
)
def test_restart_at_every_durable_boundary(tmp_path, monkeypatch, method, kind, after):
    harness = harness_at(tmp_path)

    async def scenario():
        async with harness:
            spec = await seed(harness)
            harness.adapter._responses.extend([reply()] * 4)
            original = getattr(harness.store, method)

            def crash(*args, **kwargs):
                if kind is not None and kwargs.get("kind") != kind:
                    return original(*args, **kwargs)
                if after:
                    original(*args, **kwargs)
                raise ProcessCrash()

            with monkeypatch.context() as patch:
                patch.setattr(harness.store, method, crash)
                with pytest.raises(ProcessCrash):
                    await produce(
                        harness.store, spec, issue_run=harness.executor.ids.issue_run
                    )
            producers = harness.store.list_thread_runs_chronological(
                thread_id=f"compact_{spec.target}"
            )
            assert len(producers) == 1
            with closing(RunStore(harness.store.db_path)) as store:
                output = await produce(
                    store, spec, issue_run=harness.executor.ids.issue_run
                )
                assert str(output.ref) == producers[0].id
                assert (
                    len(
                        store.list_thread_runs_chronological(
                            thread_id=f"compact_{spec.target}"
                        )
                    )
                    == 1
                )
                # Only a provider response lost before the Step commit can repeat.
                lost_response = (
                    method == "finish_step" and kind == "model" and not after
                )
                assert len(harness.adapter.invocations) == 7 + lost_response
                saved = store.get_run(run_id=str(output.ref))
                assert saved is not None and saved.status == "succeeded"
                assert RunHistory(store).get_compaction(spec.target) == output

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["provider", "empty", "cancel", "oversized"])
def test_failed_canceled_and_oversized_producers_never_publish(tmp_path, failure):
    harness = harness_at(tmp_path)

    async def scenario():
        async with harness:
            spec = await seed(harness)
            if failure == "oversized":
                spec = replace(
                    spec,
                    model=replace(spec.model, limit={"context": 1000, "output": 512}),
                )
            else:
                error = (
                    RuntimeError("provider offline")
                    if failure == "provider"
                    else asyncio.CancelledError()
                    if failure == "cancel"
                    else reply("")
                )
                # ScriptedModelAdapter accepts ordinary exceptions; cancellation
                # is raised directly to model task interruption.
                if failure == "cancel":

                    async def cancel(*args, **kwargs):
                        raise asyncio.CancelledError()

                    spec = replace(
                        spec,
                        adapter=type("Adapter", (), {"invoke": staticmethod(cancel)})(),
                    )
                else:
                    harness.adapter._responses.append(error)
            with pytest.raises((RuntimeError, ValueError, asyncio.CancelledError)):
                await produce(
                    harness.store, spec, issue_run=harness.executor.ids.issue_run
                )
            run = harness.store.list_thread_runs_chronological(
                thread_id=f"compact_{spec.target}"
            )[0]
            assert run.status == ("canceled" if failure == "cancel" else "failed")
            assert RunHistory(harness.store).get_compaction(spec.target) is None
            with pytest.raises(ValueError, match="successful root"):
                harness.store.publish_compaction(RunRef(run.id), roots=spec.roots)
            assert len(harness.adapter.invocations) == (
                4 if failure in {"cancel", "oversized"} else 5
            )

    asyncio.run(scenario())


def test_context_rejection_records_attempt_and_retries_complete_roots(
    tmp_path, monkeypatch
):
    harness = harness_at(tmp_path)
    monkeypatch.setattr(reducer, "estimate_model_input_tokens", lambda call, model: 100)

    async def scenario():
        async with harness:
            spec = await seed(harness)
            harness.adapter._responses.extend(
                [
                    ModelResponseError(
                        "context_length_exceeded",
                        kind="provider_rejection",
                        usage=ModelUsage(input_tokens=100, output_tokens=0),
                    ),
                    reply("first"),
                    reply("final"),
                ]
            )
            output = await produce(
                harness.store, spec, issue_run=harness.executor.ids.issue_run
            )
            steps = harness.store.list_steps(run_id=str(output.ref))
            assert [step.status for step in steps] == [
                "succeeded",
                "failed",
                "succeeded",
                "succeeded",
                "succeeded",
                "succeeded",
            ]
            assert steps[0].given.call.input["roots"] == [
                str(root) for root in spec.roots[:-1]
            ]
            assert steps[2].given.call.input["roots"] == [str(spec.roots[0])]
            assert steps[4].given.call.input["roots"] == [
                str(root) for root in spec.roots[1:-1]
            ]
            assert steps[1].noted.accounting.input_tokens == 100
            assert "first" in str(harness.adapter.invocations[-1].call.messages)
            assert output.result.summary == "final"

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["append", "policy", "rewind", "prior"])
def test_unpublished_success_reuse_validates_history_and_prior(
    tmp_path, monkeypatch, change
):
    harness = harness_at(tmp_path)

    async def scenario():
        async with harness:
            spec = await seed(harness)
            harness.adapter._responses.extend([reply()] * 3)
            with monkeypatch.context() as patch:

                def crash(*args, **kwargs):
                    raise ProcessCrash()

                patch.setattr(harness.store, "publish_compaction", crash)
                with pytest.raises(ProcessCrash):
                    await produce(
                        harness.store, spec, issue_run=harness.executor.ids.issue_run
                    )
            original = harness.store.list_thread_runs_chronological(
                thread_id=f"compact_{spec.target}"
            )[0]
            assert original.status == "succeeded"
            if change == "append":
                harness.adapter._responses.append(reply("appended"))
                later = await harness.executor.run(
                    harness.run_spec(
                        thread=str(spec.target),
                        runnable="chat",
                        primary=(TextPart("appended input"),),
                    )
                )
                # Even a later requested end must first reuse the completed producer.
                spec = replace(
                    spec,
                    roots=(*spec.roots, RunRef(later.id)),
                    end=RunRef(later.id),
                    versions=harness.store.history_versions(
                        [str(r) for r in (*spec.roots, RunRef(later.id))]
                    ),
                )
            elif change == "policy":
                spec = replace(spec, size=200)
            elif change == "rewind":
                harness.store.rewind_thread(
                    thread_id=str(spec.target),
                    anchor=str(spec.roots[2]),
                    request_id=None,
                    expected_head=harness.store.thread_view(str(spec.target)).head,
                    created_at="2026-01-01T00:00:00Z",
                )
            else:
                # Publish a different, shorter producer to change the prior horizon.
                harness.adapter._responses.append(reply("different prior"))
                shorter = replace(
                    spec,
                    end=spec.roots[1],
                    versions=harness.store.history_versions(
                        [str(r) for r in spec.roots[:2]]
                    ),
                )
                # The previous success can cover more than requested; make it stale
                # for this artificial branch by bypassing only candidate discovery.
                import toolang.execution.executor.compact as driver

                with monkeypatch.context() as patch:
                    patch.setattr(driver, "_candidate", lambda store, spec: None)
                    await produce(
                        harness.store, shorter, issue_run=harness.executor.ids.issue_run
                    )
            calls = len(harness.adapter.invocations)
            if change in {"append", "policy"}:
                output = await produce(
                    harness.store, spec, issue_run=harness.executor.ids.issue_run
                )
                assert str(output.ref) == original.id
            else:
                with pytest.raises((ValueError, KeyError)):
                    await produce(
                        harness.store, spec, issue_run=harness.executor.ids.issue_run
                    )
                assert harness.store.get_thread(
                    thread_id=str(spec.target)
                ).horizon != RunRef(original.id)
            assert len(harness.adapter.invocations) == calls
            if change in {"rewind", "prior"}:
                history = RunHistory(harness.store)
                roots = tuple(
                    RunRef(run.id)
                    for run in history.thread_view(
                        str(spec.target), include_children=False
                    ).roots
                )
                prior = history.get_compaction(spec.target)
                fresh = replace(
                    spec,
                    roots=roots,
                    begin=RunRef(prior.result.end) if prior else roots[0],
                    end=roots[-1],
                    summary=prior.result.summary if prior else "",
                    prior=prior.ref if prior else None,
                    versions=harness.store.history_versions(
                        [str(root) for root in roots]
                    ),
                )
                harness.adapter._responses.extend([reply("new valid summary")] * 3)
                output = await produce(
                    harness.store, fresh, issue_run=harness.executor.ids.issue_run
                )
                assert str(output.ref) != original.id
                assert output.result.summary == "new valid summary"

    asyncio.run(scenario())


def test_successful_looking_partial_producer_is_rejected_and_does_not_block(
    tmp_path, monkeypatch
):
    from toolang.execution.types import Local, Output

    harness = harness_at(tmp_path)

    async def scenario():
        async with harness:
            spec = await seed(harness)
            harness.adapter._responses.append(reply())
            original = harness.store.finish_step

            def stop_after_checkpoint(**kwargs):
                result = original(**kwargs)
                if kwargs["kind"] == "model":
                    raise ProcessCrash()
                return result

            with monkeypatch.context() as patch:
                patch.setattr(harness.store, "finish_step", stop_after_checkpoint)
                with pytest.raises(ProcessCrash):
                    await produce(
                        harness.store, spec, issue_run=harness.executor.ids.issue_run
                    )
            run = harness.store.list_thread_runs_chronological(
                thread_id=f"compact_{spec.target}"
            )[0]
            # A valid terminal record alone is insufficient to claim full coverage.
            harness.store.finish_run(
                run_id=run.id,
                status="succeeded",
                output=Output(Local.typed("Text", "cumulative summary"), "_"),
            )
            with pytest.raises(ValueError, match="incomplete checkpoint"):
                harness.store.publish_compaction(RunRef(run.id), roots=spec.roots)
            assert RunHistory(harness.store).get_compaction(spec.target) is None
            harness.adapter._responses.extend([reply("new valid summary")] * 3)
            output = await produce(
                harness.store, spec, issue_run=harness.executor.ids.issue_run
            )
            assert str(output.ref) != run.id
            assert output.result.summary == "new valid summary"

    asyncio.run(scenario())


def test_stale_running_producer_becomes_terminal_before_new_attempt(
    tmp_path, monkeypatch
):
    harness = harness_at(tmp_path)

    async def scenario():
        async with harness:
            spec = await seed(harness)
            with monkeypatch.context() as patch:

                def crash(**kwargs):
                    raise ProcessCrash()

                patch.setattr(harness.store, "begin_run", crash)
                with pytest.raises(ProcessCrash):
                    await produce(
                        harness.store, spec, issue_run=harness.executor.ids.issue_run
                    )
            old = harness.store.list_thread_runs_chronological(
                thread_id=f"compact_{spec.target}"
            )[0]
            assert old.status == "pending"
            harness.adapter._responses.extend([reply()] * 3)
            output = await produce(
                harness.store,
                replace(spec, size=200),
                issue_run=harness.executor.ids.issue_run,
            )
            assert str(output.ref) != old.id
            abandoned = harness.store.get_run(run_id=old.id)
            assert abandoned is not None and abandoned.status == "failed"
            control = harness.store.get_run_control(run_id=old.id, index=0)
            assert control is not None and control.status == "wontapply"
            harness.store.require_idle_compactor(str(spec.target))

    asyncio.run(scenario())


def test_resume_restores_provider_accounting_and_enforces_run_limits(
    tmp_path, monkeypatch
):
    harness = harness_at(tmp_path)

    async def scenario():
        async with harness:
            spec = await seed(harness)
            spec = replace(spec, limits=replace(spec.limits, tokens=46))
            harness.adapter._responses.append(reply())  # 42 + 5 exceeds the limit.
            original = harness.store.finish_step

            def crash_before_accounting(**kwargs):
                result = original(**kwargs)
                if kwargs["kind"] == "model":
                    raise ProcessCrash()
                return result

            with monkeypatch.context() as patch:
                patch.setattr(harness.store, "finish_step", crash_before_accounting)
                with pytest.raises(ProcessCrash):
                    await produce(
                        harness.store, spec, issue_run=harness.executor.ids.issue_run
                    )
            with pytest.raises(ToolangError, match="Run token limit exceeded"):
                await produce(
                    harness.store, spec, issue_run=harness.executor.ids.issue_run
                )
            assert len(harness.adapter.invocations) == 5
            run = harness.store.list_thread_runs_chronological(
                thread_id=f"compact_{spec.target}"
            )[0]
            assert run.status == "failed"
            assert RunHistory(harness.store).get_compaction(spec.target) is None

    asyncio.run(scenario())


def test_reader_preserves_recorded_tool_exchange_once(tmp_path):
    from tests.support.execution_harness import RecordingTool
    from toolang.base.types.message import ToolCallPart, ToolResultPart
    from toolang.base.types.run import ToolCall

    tool = RecordingTool("lookup__read", output={"fact": "verified"})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(tool_calls=(ToolCall("lookup", "lookup", tool.name, {}),)),
            reply("tool completed"),
            reply("middle"),
            reply("later"),
            reply("retained"),
        ],
    )

    async def scenario():
        async with harness:
            spec = await seed(harness)
            harness.adapter._responses.extend([reply()] * 3)
            await produce(harness.store, spec, issue_run=harness.executor.ids.issue_run)
            call = harness.adapter.invocations[5].call
            exchange = [
                part
                for message in call.messages
                for part in message.parts
                if isinstance(part, (ToolCallPart, ToolResultPart))
            ]
            assert [type(part) for part in exchange] == [ToolCallPart, ToolResultPart]
            assert [part.call_id for part in exchange] == ["lookup", "lookup"]
            assert "verified" in str(exchange)
            assert "input 0" in str(call.messages)
            assert "input 0" not in str(harness.adapter.invocations[6].call.messages)
            assert len(tool.calls) == 1

    asyncio.run(scenario())
