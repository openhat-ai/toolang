"""The same recovery contract for run, thread, and agent subscriptions."""

import asyncio

import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.execution.events import RunBegin, RunSnapshot
from toolang.execution.stream import StreamLimits
from toolang.execution.stream_client import StreamClientState
from toolang.execution.subscriptions import StreamScope, Subscriptions
from toolang.execution.types import ThreadPrefix


async def drain(subscription):
    frames = []
    while True:
        try:
            frames.append(await asyncio.wait_for(subscription.receive(), 2))
        except StopAsyncIteration:
            return frames


def test_completed_root_prefill_has_final_values_and_one_checkpoint(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="example", primary=(TextPart("input"),)
                )
            )
            service = Subscriptions(harness.executor.stream, harness.store.db_path)
            reservation = await service.reserve()
            sub = reservation.attach(StreamScope(root=run.id))
            frames = await drain(sub)
            reservation.close()
            assert [f.event for f in frames] == [
                "stream_prefill",
                "run_begin",
                "step_begin",
                "step_end",
                "run_end",
                "stream_checkpoint",
            ]
            assert all(f.id is None for f in frames[:-1])
            assert frames[-1].id == str(harness.executor.stream.tail)
            client = StreamClientState()
            applied = [item for frame in frames for item in client.feed(frame)]
            assert len(applied) == 1 and isinstance(applied[0], RunSnapshot)
            assert client.complete(run.id)
            assert client.cursor == frames[-1].id
            assert frames[3].data["output"] is not None

    asyncio.run(scenario())


@pytest.mark.parametrize("scope_kind", ["run", "thread", "agent"])
def test_resume_recovers_completed_trees_after_cache_loss(tmp_path, scope_kind):
    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )
    source = harness.executor.stream
    source.limits = StreamLimits(events=2)

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            before = str(source.tail)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="example", primary=(TextPart("input"),)
                )
            )
            scope = (
                StreamScope(root=run.id)
                if scope_kind == "run"
                else StreamScope(thread=thread)
                if scope_kind == "thread"
                else StreamScope()
            )
            service = Subscriptions(source, harness.store.db_path)
            reservation = await service.reserve(before)
            sub = reservation.attach(scope)
            prefix = []
            while not prefix or prefix[-1].event != "stream_checkpoint":
                prefix.append(await sub.receive())
            assert prefix[0].event == "stream_prefill"
            assert prefix[0].data["roots"] == [run.id]
            assert any(f.event == "run_end" and f.data["run"] == run.id for f in prefix)
            reservation.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("scope_kind", ["run", "thread", "agent"])
def test_concurrent_roots_keep_scopes_and_client_lifetimes_independent(
    tmp_path, scope_kind
):
    from tests.support.execution_harness import AsyncGate, ScriptedModelTurn

    gates = [AsyncGate() for _ in range(3)]
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic example:\n  Work\n",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("done")), gate=gate
            )
            for gate in gates
        ],
    )

    async def scenario():
        async with harness:
            one, two = [
                harness.threads.create(prefix=ThreadPrefix.TERM) for _ in range(2)
            ]
            handles = []
            try:
                for thread, gate in zip((one, one, two), gates):
                    handles.append(
                        harness.executor.run(
                            harness.run_spec(
                                thread=thread,
                                runnable="example",
                                primary=(TextPart("input"),),
                            )
                        )
                    )
                    await asyncio.wait_for(gate.wait_until_entered(), 2)
                expected = {
                    handle.run_id
                    for handle in handles[
                        : {"run": 1, "thread": 2, "agent": 3}[scope_kind]
                    ]
                }
                scope = (
                    StreamScope(root=handles[0].run_id)
                    if scope_kind == "run"
                    else StreamScope(thread=one)
                    if scope_kind == "thread"
                    else StreamScope()
                )
                service = Subscriptions(harness.executor.stream, harness.store.db_path)
                attachment = await service.reserve()
                sub = attachment.attach(scope)
                detached = await service.reserve()
                detached.attach(scope)
                detached.close()
                begins = set()
                while True:
                    frame = await sub.receive()
                    if frame.event == "run_begin":
                        begins.add(frame.data["run"])
                    if frame.event == "stream_checkpoint":
                        break
                assert begins == expected
                for gate in gates:
                    gate.release()
                results = await asyncio.gather(*handles)
                assert all(result.status == "succeeded" for result in results)
                ended = set()
                while ended != expected:
                    frame = await asyncio.wait_for(sub.receive(), 2)
                    if frame.event == "run_end":
                        assert frame.data["run"] in expected
                        ended.add(frame.data["run"])
                if scope_kind == "run":
                    with pytest.raises(StopAsyncIteration):
                        await sub.receive()
                else:
                    waiting = asyncio.create_task(sub.receive())
                    await asyncio.sleep(0)
                    assert not waiting.done()
                    attachment.close()
                    with pytest.raises(StopAsyncIteration):
                        await asyncio.wait_for(waiting, 2)
                attachment.close()
                assert not harness.executor.stream._readers
            finally:
                for gate in gates:
                    gate.release()

    asyncio.run(scenario())


def test_post_retry_reservation_starts_with_mutation_not_old_end(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic example:\n  Work\n",
        responses=[
            RuntimeError("fail"),
            ModelCallResult(message=Message.assistant("ok")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            first = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="example", primary=(TextPart("input"),)
                )
            )
            service = Subscriptions(harness.executor.stream, harness.store.db_path)
            reservation = await service.reserve()
            handle = harness.executor.retry(
                first.id, setup=harness.setup, state=harness.state
            )
            sub = reservation.attach(StreamScope(root=first.id), start="retry")
            frames = await drain(sub)
            assert frames[0].event == "run_retried"
            assert frames[-1].event == "run_end"
            assert frames[-1].data["status"] == "succeeded"
            assert (await handle).status == "succeeded"
            reservation.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("attach_pending", [False, True])
def test_root_stream_drains_queued_retry_past_terminal_boundary(
    tmp_path, attach_pending
):
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic example:\n  Work\n",
        responses=[
            RuntimeError("fail"),
            ModelCallResult(message=Message.assistant("ok")),
        ],
    )

    async def scenario():
        async with harness:
            source = harness.executor.stream
            source.limits = StreamLimits(batch_events=1)
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            service = Subscriptions(source, harness.store.db_path)
            reservation = await service.reserve()
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="example", primary=(TextPart("input"),)
                )
            )
            if attach_pending:
                sub = reservation.attach(StreamScope(root=handle.run_id), start="new")
            first = await handle
            if not attach_pending:
                sub = reservation.attach(StreamScope(root=first.id))
            retried = await harness.executor.retry(
                first.id, setup=harness.setup, state=harness.state
            )
            assert retried.status == "succeeded"
            frames = await drain(sub)
            assert any(frame.event == "run_retried" for frame in frames)
            assert frames[-1].event == "run_end"
            assert frames[-1].data["status"] == "succeeded"
            reservation.close()

    asyncio.run(scenario())


def test_snapshot_does_not_hold_main_store_or_publication_gate(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            first = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="example", primary=(TextPart("input"),)
                )
            )
            service = Subscriptions(harness.executor.stream, harness.store.db_path)
            reservation = await service.reserve()
            sub = reservation.attach(StreamScope(root=first.id))
            boundary = sub.boundary
            # Keep the snapshot pinned while a writer completes an entire run.
            second = await asyncio.wait_for(
                harness.executor.run(
                    harness.run_spec(
                        thread=thread, runnable="example", primary=(TextPart("input"),)
                    )
                ),
                2,
            )
            assert second.status == "succeeded"
            frames = await drain(sub)
            assert frames[-1].id == str(boundary)
            assert all(f.data.get("run") != second.id for f in frames)
            reservation.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("resume", [False, True])
def test_mid_step_attachment_fills_ancestors_and_ignores_partial_parts(
    tmp_path, resume
):
    from tests.support.execution_harness import AsyncGate, ScriptedModelTurn
    from toolang.base.types.message import TextDelta
    from toolang.base.types.run import ModelPartStart, ModelPartDelta
    from toolang.execution.events import PartDelta

    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic example:\n  Work\n",
        streaming=True,
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("partial complete")),
                updates=(
                    ModelPartStart(0, "text"),
                    ModelPartDelta(0, TextDelta("partial")),
                ),
                after_updates_gate=gate,
            )
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="example", primary=(TextPart("input"),)
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            source = harness.executor.stream
            cursor = str(source.tail) if resume else None
            service = Subscriptions(source, harness.store.db_path)
            attachment = await service.reserve(cursor)
            sub = attachment.attach(StreamScope(root=handle.run_id))
            prefix = []
            while not prefix or prefix[-1].event != "stream_checkpoint":
                prefix.append(await sub.receive())
            structural = [f for f in prefix if not f.event.startswith("stream_")]
            assert [f.event for f in structural] == ["run_begin", "step_begin"]
            assert all(f.id is None for f in structural)
            step = harness.store.list_steps(run_id=handle.run_id)[0]
            with source.publication() as publication:
                publication.append(
                    PartDelta(step.ref, 0, TextDelta("late")),
                    thread_id=thread,
                    root_run_id=handle.run_id,
                )
            gate.release()
            suffix = await drain(sub)
            assert [f.event for f in suffix] == ["step_end", "run_end"]
            assert (await handle).status == "succeeded"
            attachment.close()

    asyncio.run(scenario())


def test_cached_replay_drops_finalized_parts_without_replacing_records(tmp_path):
    from tests.support.execution_harness import ScriptedModelTurn
    from toolang.base.types.message import TextDelta
    from toolang.base.types.run import ModelPartStart, ModelPartDelta

    harness = ExecutionHarness.create(
        tmp_path,
        source="agic example:\n  Work\n",
        streaming=True,
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("partial complete")),
                updates=(
                    ModelPartStart(0, "text"),
                    ModelPartDelta(0, TextDelta("partial")),
                ),
            )
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            before = str(harness.executor.stream.tail)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="example", primary=(TextPart("input"),)
                )
            )
            service = Subscriptions(harness.executor.stream, harness.store.db_path)
            attachment = await service.reserve(before)
            frames = await drain(attachment.attach(StreamScope(root=run.id)))
            assert [f.event for f in frames] == [
                "run_begin",
                "step_begin",
                "step_end",
                "run_end",
                "stream_checkpoint",
            ]
            assert all(f.id for f in frames)
            attachment.close()

    asyncio.run(scenario())


def test_agent_initialization_omits_history_but_epoch_reset_replaces_it(tmp_path):
    from toolang.execution.stream import CanonicalStream

    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="example", primary=(TextPart("input"),)
                )
            )
            source = CanonicalStream()
            service = Subscriptions(source, harness.store.db_path)
            for after in (None, str(harness.executor.stream.tail)):
                attachment = await service.reserve(after)
                sub = attachment.attach(StreamScope())
                frames = []
                while not frames or frames[-1].event != "stream_checkpoint":
                    frames.append(await sub.receive())
                roots = [f.data["run"] for f in frames if f.event == "run_begin"]
                assert roots == ([] if after is None else [run.id])
                if after is not None:
                    assert frames[0].data["roots"] is None
                attachment.close()

    asyncio.run(scenario())


def test_retry_resume_replaces_deleted_child_and_reused_step_incarnations(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow example:
  let prefix = Retained
  run child
agic child:
  Work
""",
        responses=[
            RuntimeError("failed"),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="example", primary=(TextPart("input"),)
                )
            )
            source = harness.executor.stream
            service = Subscriptions(source, harness.store.db_path)
            client = StreamClientState()
            first = await service.reserve()
            for frame in await drain(first.attach(StreamScope(root=run.id))):
                client.feed(frame)
            first.close()
            old_children = {
                event.run
                for event in client.snapshot().events
                if isinstance(event, RunBegin) and event.parent is not None
            }
            reservation = await service.reserve(client.cursor)
            handle = harness.executor.retry(
                run.id, setup=harness.setup, state=harness.state
            )
            sub = reservation.attach(StreamScope(root=run.id), start="retry")
            frames = await drain(sub)
            assert frames[0].event == "stream_prefill"
            for frame in frames:
                client.feed(frame)
            current = client.snapshot().events
            assert not any(
                isinstance(event, RunBegin) and event.run in old_children
                for event in current
            )
            assert client.complete(run.id)
            assert (await handle).status == "succeeded"
            reservation.close()

    asyncio.run(scenario())


def test_shutdown_wakes_idle_subscription_without_closing_canonical_source(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        async with harness:
            service = Subscriptions(harness.executor.stream, harness.store.db_path)
            attachment = await service.reserve()
            sub = attachment.attach(StreamScope())
            while (await sub.receive()).event != "stream_checkpoint":
                pass
            waiting = asyncio.create_task(sub.receive())
            await asyncio.sleep(0)
            service.stop()
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(waiting, 1)
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="example", primary=(TextPart("input"),)
                )
            )
            assert run.status == "succeeded"
            assert not harness.executor.stream._readers

    asyncio.run(scenario())


def test_overflow_wins_over_an_already_terminal_record(tmp_path):
    from toolang.execution.errors import StreamOverflowError
    from toolang.execution.events import ThreadCreated
    from toolang.execution.records import ThreadPeer
    from toolang.execution.types import ControlRef

    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="example", primary=(TextPart("input"),)
                )
            )
            source = harness.executor.stream
            service = Subscriptions(source, harness.store.db_path)
            attachment = await service.reserve()
            sub = attachment.attach(StreamScope(root=run.id))
            source.limits = StreamLimits(events=2)
            for index in range(4):
                with source.publication() as publication:
                    publication.append(
                        ThreadCreated(
                            f"term_other{index}",
                            ControlRef.for_thread(f"term_other{index}", 0),
                            "chat",
                            ThreadPeer(),
                            "",
                        )
                    )
            with pytest.raises(StreamOverflowError):
                await sub.receive()
            attachment.close()
            stored = harness.store.get_run(run_id=run.id)
            assert stored is not None and stored.status == "succeeded"
            assert not source._readers

    asyncio.run(scenario())


def test_root_stream_waits_for_a_child_after_recorded_root_end(tmp_path):
    from tests.support.execution_fixtures import project_run_start, project_step
    from toolang.execution.events import RunEnd
    from toolang.execution.store import RunStore
    from toolang.execution.stream import CanonicalStream
    from toolang.execution.types import StepRef

    store = RunStore(tmp_path / "runs.db")
    project_run_start(
        store,
        run_id="run_root",
        thread_id="term_tree",
        origin="chat",
        input=Message.user("root"),
    )
    project_step(
        store,
        run_id="run_root",
        step_index=0,
        kind="run",
        status="running",
        input=(),
        output=(),
        started_at="",
        finished_at=None,
    )
    project_run_start(
        store,
        run_id="run_child",
        thread_id="term_tree",
        origin="chat",
        input=Message.user("child"),
        parent=StepRef.parse("run_root.0"),
        root_run_id="run_root",
    )
    store.finish_run(run_id="run_root", status="succeeded")

    async def scenario():
        source = CanonicalStream()
        service = Subscriptions(source, store.db_path)
        attachment = await service.reserve()
        sub = attachment.attach(StreamScope(root="run_root"))
        client = StreamClientState()
        while True:
            frame = await sub.receive()
            client.feed(frame)
            if frame.event == "stream_checkpoint":
                break
        assert not client.complete("run_root")
        assert not any(
            isinstance(event, RunEnd) and event.run == "run_root"
            for event in client.snapshot().events
        )
        pending = asyncio.create_task(sub.receive())
        await asyncio.sleep(0)
        assert not pending.done()
        with source.publication() as publication:
            store.finish_run(run_id="run_child", status="succeeded")
            publication.append(
                RunEnd("run_child", "succeeded"),
                thread_id="term_tree",
                root_run_id="run_root",
            )
        end = await asyncio.wait_for(pending, 1)
        assert end.data["run"] == "run_child"
        client.feed(end)
        assert client.complete("run_root")
        with pytest.raises(StopAsyncIteration):
            await sub.receive()
        attachment.close()
        store.close()

    asyncio.run(scenario())


def test_thread_prefill_recovers_fork_and_rewind_after_cache_loss(tmp_path):
    from toolang.execution.threads import ThreadManager

    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        async with harness:
            source = harness.executor.stream
            threads = ThreadManager(harness.store, harness.ids, stream=source)
            thread = threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="example", primary=(TextPart("input"),)
                )
            )
            after = str(source.tail)
            source.limits = StreamLimits(events=1)
            fork = threads.fork(thread_id=thread)
            threads.rewind(thread_id=thread, run_id=run.id)
            service = Subscriptions(source, harness.store.db_path)
            attachment = await service.reserve(after)
            sub = attachment.attach(StreamScope(thread=thread))
            frames = []
            while not frames or frames[-1].event != "stream_checkpoint":
                frames.append(await sub.receive())
            mutations = [
                f for f in frames if f.event in {"thread_forked", "thread_rewound"}
            ]
            assert [f.event for f in mutations] == ["thread_forked", "thread_rewound"]
            assert mutations[0].data["thread"] == fork
            assert mutations[1].data["ejected_runs"] == [run.id]
            client = StreamClientState()
            applied = [event for frame in frames for event in client.feed(frame)]
            assert [event.type for event in applied][:2] == [
                "thread_forked",
                "thread_rewound",
            ]
            attachment.close()

    asyncio.run(scenario())


def test_snapshot_byte_limit_fails_without_checkpoint_or_execution_failure(
    tmp_path, monkeypatch
):
    from toolang.execution import subscriptions
    from toolang.execution.subscriptions import SnapshotLimitError

    harness = ExecutionHarness.create(
        tmp_path,
        source="flow example:\n  let result = " + "x" * 8192 + "\n",
        responses=[],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            spec = harness.run_spec(
                thread=thread, runnable="example", primary=(TextPart("input"),)
            )
            run = await harness.executor.run(spec)
            monkeypatch.setattr(subscriptions, "SNAPSHOT_BYTES", 4096)
            service = Subscriptions(harness.executor.stream, harness.store.db_path)
            attachment = await service.reserve()
            sub = attachment.attach(StreamScope(root=run.id))
            with pytest.raises(SnapshotLimitError):
                await sub.receive()
            assert sub.checkpoint() is None
            attachment.close()
            assert not harness.executor.stream._readers
            assert (await harness.executor.run(spec)).status == "succeeded"

    asyncio.run(scenario())


def test_unconsumed_snapshot_expires_and_releases_its_connection(tmp_path, monkeypatch):
    import sqlite3
    from toolang.execution import subscriptions
    from toolang.execution.subscriptions import SnapshotLimitError

    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        async with harness:
            monkeypatch.setattr(subscriptions, "SNAPSHOT_SECONDS", 0.01)
            service = Subscriptions(harness.executor.stream, harness.store.db_path)
            attachment = await service.reserve()
            sub = attachment.attach(StreamScope())
            await asyncio.sleep(0.03)
            assert not harness.executor.stream._readers
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                sub.store.get_run(run_id="run_absent")
            with pytest.raises(SnapshotLimitError, match="expired"):
                await sub.receive()
            attachment.close()

    asyncio.run(scenario())
