"""Execution correctness scenarios spanning multiple local processes."""

from __future__ import annotations

import asyncio
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from tests.support.execution_fixtures import project_run_end, project_run_start
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.common.ids import IdIssuer
from toolang.execution.executor import RunExecutor
from toolang.execution.records import ThreadControlRef
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ThreadPrefix
from toolang.lang.input import perceive_input

_CHAT_SOURCE = """
agic chat(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
"""


def _accept_remote_steer(db_path: str, run_id: str) -> None:
    store = RunStore(Path(db_path))
    try:
        store.accept_run_control(
            run_id=run_id,
            kind="steer",
            timing="next_call",
            input=Message.user("Use the remote guidance."),
            context={"source": "remote-process"},
            request_id="remote-steer",
            created_at="2026-01-01T00:00:01Z",
        )
    finally:
        store.close()


def _cancel_remote_control(
    db_path: str,
    id_path: str,
    run_id: str,
    index: int,
) -> None:
    store = RunStore(Path(db_path))
    try:
        executor = RunExecutor(store, IdIssuer(Path(id_path)))
        executor.cancel_control(run_id=run_id, index=index)
    finally:
        store.close()


def _accept_duplicate_request(
    db_path: str,
    run_id: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    store = RunStore(Path(db_path))
    try:
        ready.put(run_id)
        start.wait()
        control = store.accept_run_control(
            run_id=run_id,
            kind="steer",
            timing="next_step",
            input=Message.user(run_id),
            context={},
            request_id="shared-control-request",
            created_at="2026-01-01T00:00:01Z",
        )
        results.put(("accepted", run_id, control.index))
    except ValueError as exc:
        results.put(("rejected", run_id, str(exc)))
    finally:
        store.close()


def _fork_thread(
    db_path: str,
    id_path: str,
    source_thread: str,
    anchor_run: str,
    request_id: str,
) -> str:
    store = RunStore(Path(db_path))
    try:
        manager = ThreadManager(store, IdIssuer(Path(id_path)))
        return manager.fork(
            thread_id=source_thread,
            run_id=anchor_run,
            request_id=request_id,
        )
    finally:
        store.close()


def _race_start(
    db_path: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    store = RunStore(Path(db_path))
    try:
        ready.put("start")
        start.wait()
        store.accept_start(
            run_id="run_racing_start",
            parent=None,
            thread="term_race",
            input=Message.user("new input"),
            context={"runnable": {"kind": "agic", "name": "chat"}},
            request_id="racing-start",
            created_at="2026-01-01T00:00:03Z",
        )
        results.put(("start", "accepted"))
    except ValueError as exc:
        results.put(("start", f"rejected:{exc}"))
    finally:
        store.close()


def _race_rewind(
    db_path: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    store = RunStore(Path(db_path))
    try:
        ready.put("rewind")
        start.wait()
        _thread, control, superseded = store.rewind_thread(
            thread_id="term_race",
            anchor=None,
            request_id="racing-rewind",
            expected_head=ThreadControlRef("term_race", 0),
            context={},
            created_at="2026-01-01T00:00:04Z",
        )
        results.put(("rewind", "accepted", control.index, superseded))
    except ValueError as exc:
        results.put(("rewind", f"rejected:{exc}"))
    finally:
        store.close()


def _race_cancel_control(
    db_path: str,
    run_id: str,
    index: int,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    store = RunStore(Path(db_path))
    try:
        ready.put(run_id)
        start.wait()
        control = store.cancel_run_control(
            run_id=run_id,
            index=index,
            canceled_at="2026-01-01T00:00:02Z",
        )
        results.put(("canceled", control.status))
    except ValueError as exc:
        results.put(("rejected", str(exc)))
    finally:
        store.close()


def _race_claim_control(
    db_path: str,
    run_id: str,
    index: int,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    store = RunStore(Path(db_path))
    try:
        ready.put(run_id)
        start.wait()
        claimed = store.claim_run_controls(run_id=run_id, indexes=(index,))
        results.put(("claimed" if index in claimed else "skipped",))
    finally:
        store.close()


def _race_processes(
    *targets: tuple[Any, tuple[object, ...]],
) -> list[tuple[object, ...]]:
    context = get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=target,
            args=(*args, ready, start, results),
        )
        for target, args in targets
    ]
    for process in processes:
        process.start()
    try:
        for _ in processes:
            ready.get(timeout=10)
        start.set()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        return [results.get(timeout=2) for _ in processes]
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join()
        for queue in (ready, results):
            queue.close()
            queue.join_thread()


def test_remote_process_can_steer_an_owned_run(tmp_path: Path) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_CHAT_SOURCE,
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("draft")),
                gate=gate,
            ),
            ModelCallResult(message=Message.assistant("final")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="chat",
                    input=perceive_input("initial input"),
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)

            process = get_context("spawn").Process(
                target=_accept_remote_steer,
                args=(str(harness.store.db_path), handle.run_id),
            )
            process.start()
            await asyncio.to_thread(process.join, 10)
            assert process.exitcode == 0

            gate.release()
            record = await asyncio.wait_for(handle, timeout=2)
            assert record.status == "finished"
            assert harness.store.run_output_text(run_id=record.id) == "final"
            assert len(harness.adapter.invocations) == 2
            assert harness.adapter.invocations[1].call.messages[-1] == Message.user(
                "Use the remote guidance."
            )
            control = harness.store.get_run_control(run_id=record.id, index=1)
            assert control is not None
            assert control.status == "finished"
            assert control.context == {"source": "remote-process"}

    asyncio.run(scenario())


def test_remote_process_can_cancel_a_pending_steer(tmp_path: Path) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_CHAT_SOURCE,
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("draft")),
                gate=gate,
            ),
            ModelCallResult(message=Message.assistant("revised")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="chat",
                    input=perceive_input("initial input"),
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            steer = handle.steer(
                Message.user("replace the draft"),
                timing="next_call",
            )

            process = get_context("spawn").Process(
                target=_cancel_remote_control,
                args=(
                    str(harness.store.db_path),
                    str(harness.ids.state_path),
                    handle.run_id,
                    steer.index,
                ),
            )
            process.start()
            await asyncio.to_thread(process.join, 10)
            assert process.exitcode == 0

            gate.release()
            record = await asyncio.wait_for(handle, timeout=2)
            assert record.status == "finished"
            assert harness.store.run_output_text(run_id=record.id) == "draft"
            assert len(harness.adapter.invocations) == 1
            stored = harness.store.get_run_control(
                run_id=record.id,
                index=steer.index,
            )
            assert stored is not None
            assert stored.status == "canceled"

    asyncio.run(scenario())


def test_duplicate_run_control_request_has_one_process_winner(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runs.db"
    store = RunStore(db_path)
    try:
        store.create_thread(thread_id="term_requests")
        for run_id in ("run_request_a", "run_request_b"):
            store.accept_start(
                run_id=run_id,
                parent=None,
                thread="term_requests",
                input=Message.user(run_id),
                context={},
                request_id=None,
                created_at="2026-01-01T00:00:00Z",
            )
    finally:
        store.close()

    outcomes = _race_processes(
        (_accept_duplicate_request, (str(db_path), "run_request_a")),
        (_accept_duplicate_request, (str(db_path), "run_request_b")),
    )

    assert [outcome[0] for outcome in outcomes].count("accepted") == 1
    assert [outcome[0] for outcome in outcomes].count("rejected") == 1
    reopened = RunStore(db_path)
    try:
        controls = [
            control
            for run_id in ("run_request_a", "run_request_b")
            for control in reopened.list_run_controls(run_id=run_id)
            if control.request_id == "shared-control-request"
        ]
        assert len(controls) == 1
        assert controls[0].index == 1
    finally:
        reopened.close()


def test_pending_control_has_one_cross_process_cancellation_winner(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runs.db"
    store = RunStore(db_path)
    try:
        store.create_thread(thread_id="term_cancel_race")
        store.accept_start(
            run_id="run_cancel_race",
            parent=None,
            thread="term_cancel_race",
            input=Message.user("hello"),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:00Z",
        )
        control = store.accept_run_control(
            run_id="run_cancel_race",
            kind="steer",
            timing="next_step",
            input=Message.user("updated"),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:01Z",
        )
    finally:
        store.close()

    outcomes = _race_processes(
        (
            _race_cancel_control,
            (str(db_path), "run_cancel_race", control.index),
        ),
        (
            _race_cancel_control,
            (str(db_path), "run_cancel_race", control.index),
        ),
    )

    assert [outcome[0] for outcome in outcomes].count("canceled") == 1
    assert [outcome[0] for outcome in outcomes].count("rejected") == 1
    reopened = RunStore(db_path)
    try:
        stored = reopened.get_run_control(
            run_id="run_cancel_race",
            index=control.index,
        )
        assert stored is not None
        assert stored.status == "canceled"
    finally:
        reopened.close()


def test_control_claim_and_cross_process_cancellation_are_linearizable(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runs.db"
    store = RunStore(db_path)
    try:
        store.create_thread(thread_id="term_claim_cancel_race")
        store.accept_start(
            run_id="run_claim_cancel_race",
            parent=None,
            thread="term_claim_cancel_race",
            input=Message.user("hello"),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:00Z",
        )
        control = store.accept_run_control(
            run_id="run_claim_cancel_race",
            kind="steer",
            timing="next_step",
            input=Message.user("updated"),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:01Z",
        )
    finally:
        store.close()

    outcomes = _race_processes(
        (
            _race_claim_control,
            (str(db_path), control.run, control.index),
        ),
        (
            _race_cancel_control,
            (str(db_path), control.run, control.index),
        ),
    )
    kinds = {str(outcome[0]) for outcome in outcomes}
    assert kinds in ({"claimed", "rejected"}, {"skipped", "canceled"})

    reopened = RunStore(db_path)
    try:
        stored = reopened.get_run_control(
            run_id=control.run,
            index=control.index,
        )
        assert stored is not None
        assert stored.status == ("pending" if "claimed" in kinds else "canceled")
    finally:
        reopened.close()


def test_concurrent_forks_preserve_one_terminal_anchor(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runs.db"
    id_path = tmp_path / "ids.json"
    store = RunStore(db_path)
    try:
        project_run_start(
            store,
            run_id="run_fork_anchor",
            thread_id="term_fork_source",
            origin="chat",
            input=Message.user("source"),
        )
        project_run_end(store, run_id="run_fork_anchor")
    finally:
        store.close()

    context = get_context("spawn")
    with context.Pool(4) as pool:
        forked = pool.starmap(
            _fork_thread,
            [
                (
                    str(db_path),
                    str(id_path),
                    "term_fork_source",
                    "run_fork_anchor",
                    f"fork-request-{index}",
                )
                for index in range(8)
            ],
        )

    assert len(forked) == len(set(forked)) == 8
    reopened = RunStore(db_path)
    try:
        for thread_id in forked:
            thread = reopened.get_thread(thread_id=thread_id)
            assert thread is not None
            assert thread.created_by == ThreadControlRef(thread_id, 0)
            assert [
                run.id
                for run in reopened.list_thread_history_chronological(
                    thread_id=thread_id
                )
            ] == ["run_fork_anchor"]
        anchor = reopened.get_run(run_id="run_fork_anchor")
        assert anchor is not None
        assert anchor.ejected is None
    finally:
        reopened.close()


def test_start_and_rewind_race_is_linearizable(tmp_path: Path) -> None:
    db_path = tmp_path / "runs.db"
    store = RunStore(db_path)
    try:
        project_run_start(
            store,
            run_id="run_race_anchor",
            thread_id="term_race",
            origin="chat",
            input=Message.user("anchor"),
        )
        project_run_end(store, run_id="run_race_anchor")
    finally:
        store.close()

    outcomes = _race_processes(
        (_race_start, (str(db_path),)),
        (_race_rewind, (str(db_path),)),
    )
    by_kind = {str(outcome[0]): outcome for outcome in outcomes}

    assert by_kind["start"] == ("start", "accepted")
    reopened = RunStore(db_path)
    try:
        new_run = reopened.get_run(run_id="run_racing_start")
        thread = reopened.get_thread(thread_id="term_race")
        assert new_run is not None
        assert new_run.ejected is None
        assert thread is not None

        rewind = by_kind["rewind"]
        if rewind[1] == "accepted":
            assert rewind[2:] == (1, ("run_race_anchor",))
            assert thread.head == ThreadControlRef("term_race", 1)
            anchor = reopened.get_run(run_id="run_race_anchor")
            assert anchor is not None
            assert anchor.ejected == (ThreadControlRef("term_race", 1))
            assert [
                run.id
                for run in reopened.list_thread_history_chronological(
                    thread_id="term_race"
                )
            ] == ["run_racing_start"]
        else:
            assert str(rewind[1]).startswith("rejected:thread is running")
            assert thread.head == ThreadControlRef("term_race", 0)
            assert [
                run.id
                for run in reopened.list_thread_history_chronological(
                    thread_id="term_race"
                )
            ] == ["run_race_anchor", "run_racing_start"]
    finally:
        reopened.close()
