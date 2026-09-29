"""Internal compaction runs share ownership, events and durable recovery."""

import asyncio
import json
from contextlib import closing
from dataclasses import replace

import pytest

from tests.support.execution_assertions import assert_replayed
from tests.support.execution_harness import (
    ExecutionHarness,
    RecordingRunTracer,
    AsyncGate,
    ScriptedModelTurn,
)
from tests.support.setup import replace_materialized_setup
from toolang.base.errors import ModelResponseError
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult, ModelUsage, ToolCall
from toolang.cli.common.execution_progress import ProgressProjector
from toolang.execution import compaction, tokens
from toolang.execution.events import (
    PartBegin,
    PartEnd,
    RunBegin,
    RunEnd,
    StepBegin,
    StepEnd,
)
from toolang.execution.executor import RunExecutor
from toolang.execution.executor._persist import _PersistSink
from toolang.execution.executor.executor import _Execution
from toolang.execution.executor.runs import compact as compact_run
from toolang.execution.executor.steps import model as model_step
from toolang.execution.inspection.history import RunHistory
from toolang.execution.records import CompactControlPayload, StoredModelStepGiven
from toolang.execution.store import RunStore
from toolang.execution.types import ModelStepNoted, RunRef, ThreadPrefix, ToolStepGiven
from toolang.plugin.models.collections import ModelCollection

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


def harness_at(path):
    return ExecutionHarness.create(
        path, source=SOURCE, responses=[reply(f"output {i}") for i in range(4)]
    )


async def seed(h, monkeypatch):
    thread = h.threads.create(prefix=ThreadPrefix.TERM)
    roots = []
    for index in range(4):
        run = await h.executor.run(
            h.run_spec(
                thread=thread, runnable="chat", primary=(TextPart(f"input {index}"),)
            )
        )
        assert run.status == "succeeded"
        roots.append(RunRef(run.id))
    model = replace(
        h.setup.models_effective()[0], limit={"context": 10000, "output": 512}
    )
    h.setup = replace_materialized_setup(h.setup, models=ModelCollection((model,)))
    # Exercise the automatic path with a deterministic, explicit retained boundary.
    monkeypatch.setattr(
        model_step,
        "_boundary",
        lambda state, *args: (
            roots[-1]
            if state.execution.horizon_for(state.prepared.run.run_id) is None
            else None
        ),
    )

    def text_tokens(text):
        value = json.loads(text)
        if not isinstance(value, dict):
            return 0
        if "messages" in value:
            return 4000
        parts = value.get("parts", [])
        if parts and parts[0]["text"].startswith("<following_messages>"):
            units = json.loads(
                parts[0]["text"]
                .removeprefix("<following_messages>")
                .removesuffix("</following_messages>")
            )
            return 4000 * len(units)
        return 0

    monkeypatch.setattr(tokens, "text_tokens", text_tokens)
    return thread, tuple(roots)


def current(h, thread, *, tracer=None):
    return h.executor.run(
        h.run_spec(
            thread=thread, runnable="chat", primary=(TextPart("current input"),)
        ),
        tracer=tracer,
    )


def producers(h, thread):
    return [
        r
        for r in h.store.list_thread_runs_chronological(thread_id=thread)
        if r.parent is not None
        and h.store.get_run_control(run_id=r.id, index=0).payload.runnable
        == compaction.RUNNABLE
    ]


def test_batches_and_client_projection_share_the_normal_event_tree(
    tmp_path, monkeypatch
):
    h = harness_at(tmp_path)
    tracer = RecordingRunTracer()

    async def scenario():
        async with h:
            thread, roots = await seed(h, monkeypatch)
            h.adapter._responses.extend(
                [reply(f"internal summary {i}") for i in range(3)]
                + [reply("visible answer")]
            )
            caller = await current(h, thread, tracer=tracer)
            assert caller.status == "succeeded", caller.error
            (child,) = producers(h, thread)
            outer, following = h.store.list_steps(run_id=caller.id)
            assert child.parent == outer.ref and str(child.thread) == thread
            assert h.store.get_thread(thread_id=f"compact_{thread}") is None
            with closing(RunStore(h.store.db_path)) as store:
                history = RunHistory(store)
                output = history.get_compaction(thread)
                assert output is not None
                assert output.ref == RunRef(child.id)
                assert output.result.end == str(roots[-1])
                steps = store.list_steps(run_id=child.id)
                assert [s.kind for s in steps] == ["tool", "model"] * 3
                for index, (read, model) in enumerate(zip(steps[::2], steps[1::2])):
                    assert read.given.call.input["roots"] == [str(roots[index])]
                    assert isinstance(model.given, StoredModelStepGiven)
                    assert model.given.call.messages.head == model.ref
                    call = history.get_model_call(model.ref)
                    assert call == h.adapter.invocations[4 + index].call
                    assert not call.tools
                    assert f"input {index}" in str(call.messages)
                    assert "input 3" not in str(call.messages)
                    if index:
                        assert f"internal summary {index - 1}" in str(call.messages)
                        assert f"input {index - 1}" not in str(call.messages)
                    assert isinstance(model.noted, ModelStepNoted)
                    assert model.noted.accounting.input_tokens == 42
                (control,) = [
                    c
                    for c in store.list_run_controls(run_id=caller.id)
                    if isinstance(c.payload, CompactControlPayload)
                ]
                assert control.triggered_by == outer.ref
                assert control.ref in following.preceded_by
                assert child.id not in [
                    r.id
                    for r in history.thread_view(thread, include_children=False).roots
                ]
            begin = next(
                e
                for e in tracer.events
                if isinstance(e, RunBegin) and e.run == child.id
            )
            assert begin.runnable == "_:compact" and begin.parent == outer.ref
            assert_replayed(h.store.db_path, tracer.events)
            projector = ProgressProjector()
            text = []
            for event in tracer.events:
                update = projector.handle(event)
                text.extend(
                    row.text
                    for b in (*update.committed, *update.live)
                    for row in b.rows
                )
            rendered = "\n".join(text)
            assert (
                "visible answer" in rendered and "Compacted thread history" in rendered
            )
            assert "internal summary" not in rendered and "compact_read" not in rendered
            assert not projector._broken
            assert projector.root_metrics.runs == 2

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
        ("accept_compact_control", None, False),
        ("accept_compact_control", None, True),
    ],
)
def test_restart_at_durable_boundaries_preserves_child_and_checkpoint(
    tmp_path, monkeypatch, method, kind, after
):
    h = harness_at(tmp_path)
    captured = []
    original_invoke = compact_run.invoke

    async def capture(state, step):
        captured.append((state, step, state.execution._active))
        return await original_invoke(state, step)

    async def scenario():
        async with h:
            thread, _ = await seed(h, monkeypatch)
            h.adapter._responses.extend([reply()] * 6)
            original = getattr(h.store, method)
            committed = False
            emit = h.executor._emit_event_locked
            adopt = _Execution.compact

            async def crash_after_event(*args, **kwargs):
                result = await emit(*args, **kwargs)
                if committed:
                    raise ProcessCrash()
                return result

            def crash_after_adoption(*args, **kwargs):
                result = adopt(*args, **kwargs)
                if committed:
                    raise ProcessCrash()
                return result

            def crash(*args, **kwargs):
                nonlocal committed
                run_id = kwargs.get("run_id") or getattr(
                    kwargs.get("ref"), "run_id", None
                )
                is_child = kwargs.get("runnable") == compaction.RUNNABLE or any(
                    r.id == run_id for r in producers(h, thread)
                )
                if (
                    method not in {"publish_compaction", "accept_compact_control"}
                    and not is_child
                ) or (kind is not None and kwargs.get("kind") != kind):
                    return original(*args, **kwargs)
                if after:
                    result = original(*args, **kwargs)
                    if method == "accept_run":
                        raise ProcessCrash()
                    # Run/Step projection and publication each have an outer transaction.
                    committed = True
                    return result
                raise ProcessCrash()

            with monkeypatch.context() as patch:
                patch.setattr(compact_run, "invoke", capture)
                patch.setattr(h.store, method, crash)
                patch.setattr(h.executor, "_emit_event_locked", crash_after_event)
                patch.setattr(_Execution, "compact", crash_after_adoption)
                with pytest.raises(ProcessCrash):
                    await current(h, thread)
            (child,) = producers(h, thread)
            state, step, old_active = captured[0]
            parent = state.prepared.run
            # Restart at the same durable owner, with a new connection and execution state.
            with closing(RunStore(h.store.db_path)) as store:
                executor = RunExecutor(store, h.executor.ids)
                executor._persist = _PersistSink(store)
                active = replace(
                    old_active, task=asyncio.current_task(), controls={}, ended=set()
                )
                execution = _Execution(executor, root=parent, active=active)
                active.execution = execution
                executor._active[parent.run_id] = active
                state.execution = execution
                state.refresh_frame = None
                receipt = await original_invoke(state, step)
                output = RunHistory(store).get_compaction(thread)
                assert output is not None
                assert output.ref == RunRef(child.id)
                completed = store.get_run(run_id=child.id)
                assert completed is not None and completed.status == "succeeded"
                assert (
                    len(
                        [
                            r
                            for r in store.list_thread_runs_chronological(
                                thread_id=thread
                            )
                            if r.parent
                        ]
                    )
                    == 1
                )
                assert receipt["controls"]
            lost = method == "finish_step" and kind == "model" and not after
            assert len(h.adapter.invocations) == 7 + lost

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["provider", "empty", "oversized"])
def test_terminal_failure_never_publishes_or_calls_normal_model(
    tmp_path, monkeypatch, failure
):
    h = harness_at(tmp_path)
    tracer = RecordingRunTracer()

    async def scenario():
        async with h:
            thread, _ = await seed(h, monkeypatch)
            if failure == "oversized":
                monkeypatch.setattr(
                    tokens.TokenCounter,
                    "base",
                    lambda self, request, overhead=0: 100000,
                )
            else:
                h.adapter._responses.append(
                    RuntimeError("provider offline")
                    if failure == "provider"
                    else reply("")
                )
            caller = await current(h, thread, tracer=tracer)
            (child,) = producers(h, thread)
            assert caller.status == child.status == "failed"
            assert RunHistory(h.store).get_compaction(thread) is None
            assert [s.kind for s in h.store.list_steps(run_id=caller.id)] == ["tool"]
            assert len(h.adapter.invocations) == (4 if failure == "oversized" else 5)
            projector = ProgressProjector()
            rendered = []
            for event in tracer.events:
                rendered.extend(
                    row.text
                    for b in projector.handle(event).committed
                    for row in b.rows
                )
            assert not projector._broken
            if failure == "provider":
                assert "\n".join(rendered).count("provider offline") == 1

    asyncio.run(scenario())


def test_direct_child_cancel_is_consumed_and_stops_parent(tmp_path, monkeypatch):
    h = harness_at(tmp_path)
    gate = AsyncGate()

    async def scenario():
        async with h:
            thread, _ = await seed(h, monkeypatch)
            h.adapter._responses.append(ScriptedModelTurn(reply(), gate=gate))
            handle = current(h, thread)
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            (child,) = producers(h, thread)
            control = h.executor.cancel(run_id=child.id)
            parent = await asyncio.wait_for(handle, 2)
            assert parent.status == "canceled"
            assert h.store.get_run(run_id=child.id).status == "canceled"
            assert (
                h.store.get_run_control(run_id=child.id, index=control.index).status
                == "applied"
            )
            assert RunHistory(h.store).get_compaction(thread) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["parent", "child"])
@pytest.mark.parametrize(
    "boundary",
    [
        "run_begin",
        "tool_begin",
        "tool_end",
        "model_begin",
        "model_end",
        "tool_part_begin",
        "tool_part_end",
        "model_part_begin",
        "model_part_end",
    ],
)
def test_cancel_at_internal_event_boundaries_closes_lifecycle(
    tmp_path, monkeypatch, target, boundary
):
    h = harness_at(tmp_path)

    class CancelTracer(RecordingRunTracer):
        child = None
        parent = None
        fired = False

        async def on_event(self, event):
            self.events.append(event)
            if isinstance(event, RunBegin) and event.runnable == compaction.RUNNABLE:
                self.child = event.run
                self.parent = event.parent.run_id
            point = None
            if isinstance(event, RunBegin | RunEnd) and event.run == self.child:
                point = event.type
            elif (
                isinstance(event, StepBegin | StepEnd)
                and event.step.run_id == self.child
            ):
                point = (
                    f"{event.kind}_{'begin' if isinstance(event, StepBegin) else 'end'}"
                )
            elif (
                isinstance(event, PartBegin | PartEnd)
                and event.step.run_id == self.child
            ):
                kind = h.store.get_step(ref=event.step).kind
                point = f"{kind}_{event.type}"
            if point == boundary and not self.fired:
                self.fired = True
                h.executor.cancel(
                    run_id=self.parent if target == "parent" else self.child
                )
                # A canceled observer must not block lifecycle cleanup.
                await asyncio.Future()

    tracer = CancelTracer()

    async def scenario():
        async with h:
            thread, _ = await seed(h, monkeypatch)
            h.adapter._responses.extend([reply()] * 4)
            parent = await asyncio.wait_for(current(h, thread, tracer=tracer), 2)
            assert tracer.fired
            assert parent.status == "canceled"
            (child,) = producers(h, thread)
            assert child.status == "canceled"
            steps = h.store.list_steps(run_id=child.id)
            assert all(s.status != "running" for s in steps)
            if steps and boundary not in {"tool_end", "model_end"}:
                assert steps[-1].status == "canceled"
            assert [s.kind for s in h.store.list_steps(run_id=parent.id)] == ["tool"]
            assert RunHistory(h.store).get_compaction(thread) is None
            projector = ProgressProjector()
            for event in tracer.events:
                projector.handle(event)
                assert not projector._broken, event
            assert_replayed(h.store.db_path, tracer.events)

    asyncio.run(scenario())


def test_retry_cannot_delete_an_adopted_compaction_child(tmp_path, monkeypatch):
    h = harness_at(tmp_path)

    async def scenario():
        async with h:
            thread, _ = await seed(h, monkeypatch)
            h.adapter._responses.extend([reply()] * 4)
            owner = await current(h, thread)
            assert owner.status == "succeeded"
            (child,) = producers(h, thread)
            monkeypatch.setattr(model_step, "_boundary", lambda *args: None)
            h.adapter._responses.append(reply("later"))
            later = await current(h, thread)
            assert later.status == "succeeded"
            before = h.store.list_run_tree(root_run_id=owner.id)
            steps = h.store.list_steps(run_id=owner.id)
            controls = h.store.list_run_controls(run_id=owner.id)
            with pytest.raises(ValueError, match="referenced horizon.*use rerun"):
                await h.executor.retry(
                    owner.id, setup=h.setup, state=h.state, anchor=child.parent
                )
            assert h.store.list_run_tree(root_run_id=owner.id) == before
            assert h.store.list_steps(run_id=owner.id) == steps
            assert h.store.list_run_controls(run_id=owner.id) == controls
            assert h.store.get_run(run_id=child.id) == child
            for action in (h.executor.retry, h.executor.rerun):
                h.adapter._responses.append(reply("later again"))
                result = await action(later.id, setup=h.setup, state=h.state)
                assert result.status == "succeeded", result.error
            # The rejected owner's supported alternative retains the producer.
            h.adapter._responses.append(reply("owner again"))
            result = await h.executor.rerun(owner.id, setup=h.setup, state=h.state)
            assert result.status == "succeeded", result.error
            assert h.store.get_run(run_id=child.id) == child

    asyncio.run(scenario())


def test_retry_can_replace_an_unpublished_compaction_child(tmp_path, monkeypatch):
    h = harness_at(tmp_path)

    def fail_publication(*args):
        raise RuntimeError("publication interrupted")

    async def scenario():
        async with h:
            thread, _ = await seed(h, monkeypatch)
            h.adapter._responses.extend([reply()] * 3)
            with monkeypatch.context() as patch:
                patch.setattr(_Execution, "compact", fail_publication)
                owner = await current(h, thread)
            assert owner.status == "failed"
            (child,) = producers(h, thread)
            assert child.status == "succeeded"
            monkeypatch.setattr(model_step, "_boundary", lambda *args: None)
            h.adapter._responses.append(reply("retried"))
            retried = await h.executor.retry(owner.id, setup=h.setup, state=h.state)
            assert retried.status == "succeeded", retried.error
            assert h.store.get_run(run_id=child.id) is None

    asyncio.run(scenario())


def test_context_rejection_records_attempt_then_shrinks_only_whole_roots(
    tmp_path, monkeypatch
):
    h = harness_at(tmp_path)

    async def scenario():
        async with h:
            thread, roots = await seed(h, monkeypatch)
            monkeypatch.setattr(tokens, "text_tokens", lambda text: 0)
            h.adapter._responses.extend(
                [
                    ModelResponseError(
                        "context_length_exceeded",
                        kind="provider_rejection",
                        usage=ModelUsage(input_tokens=100, output_tokens=0),
                    ),
                    reply("first summary"),
                    reply("final summary"),
                    reply("done"),
                ]
            )
            parent = await current(h, thread)
            assert parent.status == "succeeded", parent.error
            (child,) = producers(h, thread)
            steps = h.store.list_steps(run_id=child.id)
            assert [s.status for s in steps] == [
                "succeeded",
                "failed",
                "succeeded",
                "succeeded",
                "succeeded",
                "succeeded",
            ]
            assert steps[0].given.call.input["roots"] == [str(r) for r in roots[:-1]]
            assert steps[2].given.call.input["roots"] == [str(roots[0])]
            assert steps[4].given.call.input["roots"] == [str(r) for r in roots[1:-1]]
            assert steps[1].noted.accounting.input_tokens == 100

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["_:compact", "<inner>:compact", "inner:compact"])
def test_internal_run_cannot_be_started_publicly(tmp_path, name):
    h = harness_at(tmp_path)

    async def scenario():
        async with h:
            thread = h.threads.create(prefix=ThreadPrefix.TERM)
            with pytest.raises(ValueError, match="runnable"):
                h.executor.run(
                    h.run_spec(
                        thread=thread, runnable=name, primary=(TextPart("input"),)
                    )
                )
            assert not h.store.list_thread_runs_chronological(thread_id=thread)

    asyncio.run(scenario())


def test_model_cannot_invoke_compaction_tool(tmp_path):
    h = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelCallResult(
                tool_calls=(ToolCall("compact", "compact", "_toolang__compact", {}),)
            ),
            reply("done"),
        ],
    )

    async def scenario():
        async with h:
            thread = h.threads.create(prefix=ThreadPrefix.TERM)
            parent = await current(h, thread)
            steps = h.store.list_steps(run_id=parent.id)
            failed = next(s for s in steps if s.kind == "tool")
            assert failed.status == "failed"
            assert "preflight" in str(failed.output)
            assert not producers(h, thread)

    asyncio.run(scenario())


@pytest.mark.parametrize("covered", [False, True])
def test_retry_only_invalidates_summarized_root_versions(
    tmp_path, monkeypatch, covered
):
    h = harness_at(tmp_path)

    async def scenario():
        async with h:
            thread, roots = await seed(h, monkeypatch)
            h.adapter._responses.extend([reply()] * 4)
            parent = await current(h, thread)
            assert parent.status == "succeeded", parent.error
            history = RunHistory(h.store)
            before = history.get_compaction(thread)
            assert before is not None
            monkeypatch.setattr(model_step, "_boundary", lambda *args: None)
            h.adapter._responses.append(reply("retried root"))
            retried = await h.executor.retry(
                str(roots[0] if covered else roots[-1]), setup=h.setup, state=h.state
            )
            assert retried.status == "succeeded", retried.error
            if covered:
                assert history.get_compaction(thread) is None
            else:
                assert history.get_compaction(thread) == before
            h.adapter._responses.append(reply("next answer"))
            next_run = await current(h, thread)
            assert next_run.status == "succeeded", next_run.error

    asyncio.run(scenario())


def test_rewind_does_not_leave_a_permanently_blocking_raw_horizon(
    tmp_path, monkeypatch
):
    from toolang.common.time import utc_now

    h = harness_at(tmp_path)

    async def scenario():
        async with h:
            thread, roots = await seed(h, monkeypatch)
            h.adapter._responses.extend([reply()] * 4)
            assert (await current(h, thread)).status == "succeeded"
            history = RunHistory(h.store)
            old = history.get_compaction(thread)
            assert old is not None
            record, head, _ = h.store.history_thread_members(thread)
            h.store.rewind_thread(
                thread_id=thread,
                anchor=str(roots[-1]),
                request_id=None,
                expected_head=head,
                created_at=utc_now(),
            )
            assert history.get_compaction(thread) is None
            assert h.store.get_thread(thread_id=thread).horizon == old.ref
            monkeypatch.setattr(
                model_step,
                "_boundary",
                lambda state, *args: (
                    roots[-2]
                    if state.execution.horizon_for(state.prepared.run.run_id) is None
                    else None
                ),
            )
            h.adapter._responses.extend([reply()] * 3)
            parent = await current(h, thread)
            assert parent.status == "succeeded", parent.error
            latest = history.get_compaction(thread)
            assert latest is not None and latest.ref != old.ref
            assert latest.result.end == str(roots[-2])

    asyncio.run(scenario())


def test_compaction_usage_is_charged_to_parent_tree_limit(tmp_path, monkeypatch):
    from toolang.base.types.policy import RunLimits

    h = harness_at(tmp_path)

    async def scenario():
        async with h:
            thread, _ = await seed(h, monkeypatch)
            h.adapter._responses.extend([reply()] * 4)
            parent = await h.executor.run(
                h.run_spec(
                    thread=thread,
                    runnable="chat",
                    primary=(TextPart("current input"),),
                    limits=RunLimits(tokens=60),
                )
            )
            assert parent.status == "failed"
            assert "token limit" in h.store.resolve_error(parent.error)
            (child,) = producers(h, thread)
            assert child.status == "failed"
            assert len(h.adapter.invocations) == 6
            assert RunHistory(h.store).get_compaction(thread) is None

    asyncio.run(scenario())


def test_restart_inside_one_root_keeps_accepted_step_coverage(tmp_path, monkeypatch):
    from tests.support.execution_harness import RecordingTool
    from tests.integration.execution.test_compact_scenarios import constrain
    from toolang.setup.config import resolve_compact_config

    tool = RecordingTool("lookup__read", output={"text": "large body " * 5000})
    calls = [ToolCall(str(i), str(i), tool.name, {}) for i in range(3)]
    h = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            *[ModelCallResult(tool_calls=(call,)) for call in calls],
            reply("latest Step"),
        ],
    )
    captured = []
    invoke = compact_run.invoke

    async def capture(state, step):
        captured.append((state, step, state.execution._active))
        return await invoke(state, step)

    async def scenario():
        async with h:
            thread = h.threads.create(prefix=ThreadPrefix.TERM)
            original = await current(h, thread)
            assert original.status == "succeeded"
            source_units = compaction.history_units(h.store, original)
            constrain(h)
            h.setup = replace_materialized_setup(
                h.setup,
                models=ModelCollection(
                    tuple(
                        replace(m, limit={"context": 8000, "output": 1024})
                        if m.id == "reducer"
                        else m
                        for m in h.setup.models_effective()
                    )
                ),
            )
            h.setup = replace(
                h.setup,
                compact=resolve_compact_config(
                    (
                        {
                            "compact": {
                                "model": "test/reducer",
                                "summary": 128,
                                "recent": 1,
                            }
                        },
                    )
                ),
            )
            h.adapter._responses.extend([reply("Cumulative facts.")] * 16)
            emit = h.executor._emit_event_locked

            async def crash_after_checkpoint(active, event):
                result = await emit(active, event)
                if (
                    isinstance(event, StepEnd)
                    and event.kind == "model"
                    and event.status == "succeeded"
                ):
                    raise ProcessCrash()
                return result

            with monkeypatch.context() as patch:
                patch.setattr(compact_run, "invoke", capture)
                patch.setattr(h.executor, "_emit_event_locked", crash_after_checkpoint)
                with pytest.raises(ProcessCrash):
                    await current(h, thread)
            (child,) = producers(h, thread)
            cursor, _ = compaction.read_checkpoint(h.store, child)
            assert 0 < cursor < len(source_units) - 1
            state, step, old_active = captured[0]
            with closing(RunStore(h.store.db_path)) as store:
                executor = RunExecutor(store, h.executor.ids)
                executor._persist = _PersistSink(store)
                parent = state.prepared.run
                active = replace(
                    old_active, task=asyncio.current_task(), controls={}, ended=set()
                )
                execution = _Execution(executor, root=parent, active=active)
                active.execution = execution
                executor._active[parent.run_id] = active
                state.execution = execution
                state.refresh_frame = None
                rendered = []
                render_unit = compact_run.render_history_unit

                def observe(unit, resolve):
                    rendered.append(unit.ref)
                    return render_unit(unit, resolve)

                with monkeypatch.context() as patch:
                    patch.setattr(compact_run, "render_history_unit", observe)
                    receipt = await invoke(state, step)
                assert receipt["controls"]
                assert rendered and set(rendered) == {
                    u.ref for u in source_units[cursor:-1]
                }
                completed = store.get_run(run_id=child.id)
                assert completed is not None and completed.status == "succeeded"
                reads = [
                    s.given.call.input["units"]
                    for s in store.list_steps(run_id=child.id)
                    if isinstance(s.given, ToolStepGiven) and s.status == "succeeded"
                ]
                assert [ref for batch in reads for ref in batch] == [
                    str(u.ref) for u in source_units[:-1]
                ]
                assert (
                    compaction.read_checkpoint(store, completed)[0]
                    == len(source_units) - 1
                )

    asyncio.run(scenario())
