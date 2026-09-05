"""Durable thread-control correctness scenarios."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.execution.events import (
    ThreadCreated,
    ThreadEvent,
    ThreadForked,
    ThreadListener,
    ThreadRewound,
)
from toolang.execution.records import (
    CreateControlPayload,
    ForkControlPayload,
    RewindControlPayload,
    ThreadPeer,
)
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ControlRef, RunRef, ThreadPrefix, ThreadRef
from toolang.lang.input import resolve_input_parts

_CHAT_SOURCE = """
agic chat(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
"""


class _RecordingThreadListener(ThreadListener):
    """Record only events whose thread control is already durable."""

    def __init__(self, store: RunStore) -> None:
        self.store = store
        self.events: list[ThreadEvent] = []

    def on_event(self, event: ThreadEvent) -> None:
        thread = self.store.get_thread(thread_id=event.thread)
        control = self.store.get_thread_control(
            thread_id=str(event.control.target),
            index=event.control.index,
        )
        assert thread is not None
        assert control is not None and control.status == "applied"
        self.events.append(event)


class _FailingThreadListener(ThreadListener):
    def on_event(self, event: ThreadEvent) -> None:
        raise RuntimeError(f"cannot deliver {event.type}")


def test_create_and_fork_controls_preserve_identity_and_anchor(
    tmp_path: Path,
) -> None:
    peer = ThreadPeer(type="agent", name="bob", thread="remote-thread")
    harness = ExecutionHarness.create(
        tmp_path,
        source=_CHAT_SOURCE,
        responses=[
            ModelCallResult(message=Message.assistant("first answer")),
            ModelCallResult(message=Message.assistant("second answer")),
        ],
    )
    listener = _RecordingThreadListener(harness.store)
    manager = ThreadManager(harness.store, harness.ids, listener=listener)

    async def scenario() -> tuple[str, str, str, str]:
        async with harness:
            source = manager.create(
                prefix=ThreadPrefix.WEB,
                request_id="thread-create-1",
                peer=peer,
            )
            first = await harness.executor.run(
                harness.run_spec(
                    thread=source,
                    runnable="chat",
                    primary=resolve_input_parts("first"),
                )
            )
            second = await harness.executor.run(
                harness.run_spec(
                    thread=source,
                    runnable="chat",
                    primary=resolve_input_parts("second"),
                )
            )
            forked = manager.fork(
                thread_id=source,
                run_id=first.id,
                request_id="thread-fork-1",
            )
            return source, forked, first.id, second.id

    source, forked, first_run, second_run = asyncio.run(scenario())

    assert [event.type for event in listener.events] == [
        "thread_created",
        "thread_forked",
    ]
    created_event, forked_event = listener.events
    assert isinstance(created_event, ThreadCreated)
    assert created_event.control == ControlRef.for_thread(source, 0)
    assert created_event.peer == peer
    assert isinstance(forked_event, ThreadForked)
    assert forked_event.control == ControlRef.for_thread(forked, 0)
    assert forked_event.source_thread == source
    assert forked_event.anchor_run == first_run

    reopened = RunStore(harness.store.db_path)
    try:
        source_thread = reopened.get_thread(thread_id=source)
        assert source_thread is not None
        assert source_thread.peer == peer
        assert reopened.thread_views().head(source) == ControlRef.for_thread(source, 0)
        create_control = reopened.list_thread_controls(thread_id=source)
        assert len(create_control) == 1
        assert create_control[0].kind == "create"
        assert create_control[0].request == "thread-create-1"
        assert create_control[0].payload == CreateControlPayload()
        assert create_control[0].status == "applied"
        assert create_control[0].finished_at == create_control[0].created_at

        forked_thread = reopened.get_thread(thread_id=forked)
        assert forked_thread is not None
        assert forked_thread.peer == peer
        assert reopened.thread_views().head(forked) == ControlRef.for_thread(forked, 0)
        fork_control = reopened.list_thread_controls(thread_id=forked)
        assert len(fork_control) == 1
        assert fork_control[0].kind == "fork"
        assert fork_control[0].payload == ForkControlPayload(
            fork_from=ThreadRef(source),
            fork_head=ControlRef.for_thread(source, 0),
            fork_at=RunRef(first_run),
        )
        assert fork_control[0].request == "thread-fork-1"
        assert fork_control[0].status == "applied"
        assert [
            run.id
            for run in reopened.list_thread_history_chronological(thread_id=forked)
        ] == [first_run]
        assert second_run not in {
            run.id
            for run in reopened.list_thread_history_chronological(thread_id=forked)
        }
    finally:
        reopened.close()


def test_rewind_controls_form_a_monotonic_head_chain(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=_CHAT_SOURCE,
        responses=[
            ModelCallResult(message=Message.assistant("first answer")),
            ModelCallResult(message=Message.assistant("second answer")),
            ModelCallResult(message=Message.assistant("third answer")),
            ModelCallResult(message=Message.assistant("replacement answer")),
        ],
    )
    listener = _RecordingThreadListener(harness.store)
    manager = ThreadManager(harness.store, harness.ids, listener=listener)

    async def scenario() -> None:
        async with harness:
            thread = manager.create(prefix=ThreadPrefix.TERM)
            runs = []
            for prompt in ("first", "second", "third"):
                runs.append(
                    await harness.executor.run(
                        harness.run_spec(
                            thread=thread,
                            runnable="chat",
                            primary=resolve_input_parts(prompt),
                        )
                    )
                )

            assert (
                manager.rewind(
                    thread_id=thread,
                    run_id=runs[1].id,
                    request_id="thread-rewind-1",
                )
                is None
            )
            replacement = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="chat",
                    primary=resolve_input_parts("replacement"),
                )
            )
            assert (
                manager.rewind(
                    thread_id=thread,
                    request_id="thread-rewind-2",
                )
                is None
            )

            controls = harness.store.list_thread_controls(thread_id=thread)
            assert [control.index for control in controls] == [0, 1, 2]
            assert [control.kind for control in controls] == [
                "create",
                "rewind",
                "rewind",
            ]
            assert [control.payload for control in controls] == [
                CreateControlPayload(),
                RewindControlPayload(
                    rewind_from=RunRef(runs[1].id),
                    rewind_through=RunRef(runs[2].id),
                    rewind_if=ControlRef.for_thread(thread, 0),
                ),
                RewindControlPayload(
                    rewind_from=RunRef(replacement.id),
                    rewind_through=RunRef(replacement.id),
                    rewind_if=ControlRef.for_thread(thread, 1),
                ),
            ]
            assert [control.request for control in controls] == [
                None,
                "thread-rewind-1",
                "thread-rewind-2",
            ]
            assert all(control.status == "applied" for control in controls)
            assert all(
                control.finished_at == control.created_at for control in controls
            )

            record = harness.store.get_thread(thread_id=thread)
            assert record is not None
            assert harness.store.thread_views().head(thread) == ControlRef.for_thread(
                thread, 2
            )
            first, second, third = (
                harness.store.get_run(run_id=run.id) for run in runs
            )
            replacement_record = harness.store.get_run(run_id=replacement.id)
            assert first is not None and first.ejected_by is None
            assert second is not None
            assert second.ejected_by is None
            assert third is not None
            assert third.ejected_by is None
            assert replacement_record is not None
            assert replacement_record.ejected_by is None
            assert [
                run.id
                for run in harness.store.list_thread_history_chronological(
                    thread_id=thread
                )
            ] == [runs[0].id]

            rewinds = [
                event for event in listener.events if isinstance(event, ThreadRewound)
            ]
            assert [event.control.index for event in rewinds] == [1, 2]
            assert rewinds[0].ejected_runs == (
                runs[1].id,
                runs[2].id,
            )
            assert rewinds[1].ejected_runs == (replacement.id,)

    asyncio.run(scenario())


def test_fork_accepts_an_earlier_terminal_anchor_while_source_runs(
    tmp_path: Path,
) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_CHAT_SOURCE,
        responses=[
            ModelCallResult(message=Message.assistant("first answer")),
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("second answer")),
                gate=gate,
            ),
        ],
    )
    listener = _RecordingThreadListener(harness.store)
    manager = ThreadManager(harness.store, harness.ids, listener=listener)

    async def scenario() -> None:
        async with harness:
            source = manager.create(prefix=ThreadPrefix.TERM)
            terminal = await harness.executor.run(
                harness.run_spec(
                    thread=source,
                    runnable="chat",
                    primary=resolve_input_parts("first"),
                )
            )
            active = harness.executor.run(
                harness.run_spec(
                    thread=source,
                    runnable="chat",
                    primary=resolve_input_parts("second"),
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)

            forked = manager.fork(
                thread_id=source,
                run_id=terminal.id,
                request_id="fork-terminal-anchor",
            )
            with pytest.raises(
                ValueError,
                match=f"anchor run is not terminal: {active.run_id}",
            ):
                manager.fork(thread_id=source)
            with pytest.raises(
                ValueError,
                match=f"anchor run is not terminal: {active.run_id}",
            ):
                manager.fork(thread_id=source, run_id=active.run_id)

            assert [
                run.id
                for run in harness.store.list_thread_history_chronological(
                    thread_id=forked
                )
            ] == [terminal.id]
            fork_control = harness.store.get_thread_control(
                thread_id=forked,
                index=0,
            )
            assert fork_control is not None
            assert fork_control.payload == ForkControlPayload(
                fork_from=ThreadRef(source),
                fork_head=ControlRef.for_thread(source, 0),
                fork_at=RunRef(terminal.id),
            )
            assert [event.type for event in listener.events] == [
                "thread_created",
                "thread_forked",
            ]

            active.cancel(reason="test complete")
            active_record = await asyncio.wait_for(active, timeout=2)
            assert active_record.status == "canceled"

    asyncio.run(scenario())


def test_rewind_rejects_a_running_thread_without_canceling_it(
    tmp_path: Path,
) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_CHAT_SOURCE,
        responses=[
            ModelCallResult(message=Message.assistant("first answer")),
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("second answer")),
                gate=gate,
            ),
        ],
    )
    listener = _RecordingThreadListener(harness.store)
    manager = ThreadManager(harness.store, harness.ids, listener=listener)

    async def scenario() -> None:
        async with harness:
            thread = manager.create(prefix=ThreadPrefix.TERM)
            terminal = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="chat",
                    primary=resolve_input_parts("first"),
                )
            )
            active = harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="chat",
                    primary=resolve_input_parts("second"),
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)

            with pytest.raises(
                ValueError,
                match=f"thread is running: {thread}",
            ):
                manager.rewind(thread_id=thread, run_id=terminal.id)
            with pytest.raises(
                ValueError,
                match=f"thread is running: {thread}",
            ):
                manager.rewind(thread_id=thread)

            assert [
                control.kind
                for control in harness.store.list_run_controls(run_id=active.run_id)
            ] == ["run"]
            assert [
                control.kind
                for control in harness.store.list_thread_controls(thread_id=thread)
            ] == ["create"]
            assert [event.type for event in listener.events] == ["thread_created"]

            active.cancel(reason="caller canceled before rewind")
            canceled = await asyncio.wait_for(active, timeout=2)
            assert canceled.status == "canceled"
            assert (
                manager.rewind(
                    thread_id=thread,
                    request_id="rewind-after-cancel",
                )
                is None
            )
            rewind = harness.store.get_thread_control(
                thread_id=thread,
                index=1,
            )
            assert rewind is not None
            assert rewind.payload == RewindControlPayload(
                rewind_from=RunRef(canceled.id),
                rewind_through=RunRef(canceled.id),
                rewind_if=ControlRef.for_thread(thread, 0),
            )
            assert [
                run.id
                for run in harness.store.list_thread_history_chronological(
                    thread_id=thread
                )
            ] == [terminal.id]

    asyncio.run(scenario())


def test_failed_thread_controls_leave_no_record_or_event(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=_CHAT_SOURCE,
        responses=[
            ModelCallResult(message=Message.assistant("answer")),
        ],
    )
    listener = _RecordingThreadListener(harness.store)
    manager = ThreadManager(harness.store, harness.ids, listener=listener)

    async def scenario() -> None:
        async with harness:
            thread = manager.create(
                prefix=ThreadPrefix.TERM,
                request_id="thread-request-1",
            )
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="chat",
                    primary=resolve_input_parts("question"),
                )
            )

            with pytest.raises(
                ValueError,
                match="thread control request already exists",
            ):
                manager.fork(
                    thread_id=thread,
                    request_id="thread-request-1",
                )
            with pytest.raises(
                ValueError,
                match="thread control request already exists",
            ):
                manager.rewind(
                    thread_id=thread,
                    request_id="thread-request-1",
                )
            with pytest.raises(
                ValueError,
                match="run is not visible",
            ):
                manager.fork(thread_id=thread, run_id="run_missing")
            with pytest.raises(ValueError, match="invalid request id"):
                manager.fork(
                    thread_id=thread,
                    run_id=run.id,
                    request_id=" invalid",
                )

            assert harness.store.list_thread_controls(thread_id=thread) == (
                harness.store.get_thread_control(
                    thread_id=thread,
                    index=0,
                ),
            )
            assert len(harness.store.list_threads()) == 1
            stored_run = harness.store.get_run(run_id=run.id)
            assert stored_run is not None
            assert stored_run.ejected_by is None
            assert [event.type for event in listener.events] == ["thread_created"]

    asyncio.run(scenario())


def test_listener_failure_does_not_roll_back_a_thread_control(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=_CHAT_SOURCE,
        responses=[],
    )
    manager = ThreadManager(
        harness.store,
        harness.ids,
        listener=_FailingThreadListener(),
    )

    async def scenario() -> None:
        async with harness:
            thread = manager.create(
                prefix=ThreadPrefix.WEB,
                request_id="thread-create-with-failing-listener",
            )

            record = harness.store.get_thread(thread_id=thread)
            control = harness.store.get_thread_control(
                thread_id=thread,
                index=0,
            )
            assert record is not None
            assert control is not None
            assert control.status == "applied"
            assert harness.store.thread_views().head(thread) == ControlRef.for_thread(
                thread, 0
            )

    asyncio.run(scenario())
