from toolang.catalog.chore_ids import chore_id

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
import threading

import pytest

from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.catalog.chore import AuthoredChores, ChoreFile

from toolang.execution.records import CreateControlPayload, RunControlPayload
from toolang.execution.types import ThreadKind, ThreadRef
from toolang.lang.input import CallInput
from toolang.lang.types import Array
from toolang.catalog.chore_authoring import new_chore_file
from toolang.driver.chore.records import ChoreRecord
from toolang.driver.chore.scheduler import ChoreDriver
from toolang.base.errors import ToolangError
from toolang.catalog.chores import load_chores
from toolang.driver.chore.store import ChoreStore
from toolang.state.watcher import StateWatcher
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)


_SOURCE = """
agic review(_: Part[], focus: Text):
  recall = none
  context = none
  instruct = none
  user: {{focus}} {{_}}
"""


def _result(text: str = "done") -> ModelCallResult:
    return ModelCallResult(message=Message.assistant(text))


def _create_chore(
    catalog: AuthoredChores,
    name: str,
    body: str,
) -> ChoreFile:
    return catalog.create(
        new_chore_file(
            name=name,
            title=None,
            body=body,
        )
    )


def _scheduler(harness: ExecutionHarness) -> ChoreDriver:
    watcher = StateWatcher(harness.setup.layout, initial_state=harness.state)

    class PublishedDriver(ChoreDriver):
        async def start(self) -> None:
            self.stop_signal = asyncio.Event()
            await watcher.refresh()
            harness.state = watcher.current()

            async def publish():
                async for state in watcher.updates(
                    stop_signal=self.stop_signal, interval_ms=50, debounce_ms=50
                ):
                    harness.state = state

            self.publisher = asyncio.create_task(publish())
            await super().start()

        async def refresh(self) -> None:
            await watcher.refresh()
            harness.state = watcher.current()
            await super().refresh()

        async def stop(self) -> None:
            await super().stop()
            self.stop_signal.set()
            await self.publisher

    return PublishedDriver(
        layout=harness.setup.layout,
        executor=harness.executor,
        ids=harness.ids,
        get_agent_setup=lambda: harness.setup,
        get_agent_state=lambda: watcher.current(),
        safety_refresh_seconds=60.0,
        state_poll_seconds=0.05,
    )


async def _wait_record(
    scheduler: ChoreDriver,
    name: str,
    predicate: Callable[[ChoreRecord], bool],
) -> ChoreRecord:
    async with asyncio.timeout(5):
        while True:
            record = await scheduler.get(name)
            if record is not None and predicate(record):
                return record
            await asyncio.sleep(0.01)


async def _close_scheduler(
    scheduler: ChoreDriver,
    harness: ExecutionHarness,
) -> None:
    await scheduler.pause()
    await harness.executor.stop()
    await scheduler.stop()


@pytest.mark.parametrize("operation", ["fork", "rewind"])
def test_thread_controls_preserve_completed_chore_checkpoint_on_restart(
    tmp_path, operation
):
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        prepare_state=True,
        source=_SOURCE,
        responses=[_result("First completed."), _result("Manual completed.")],
    )
    layout = harness.setup.layout
    _create_chore(
        AuthoredChores(
            layout.home, lock_path=layout.home_edit_lock, agent_name=layout.name
        ),
        "review",
        "/agic:review focus=security\n\nReview this.",
    )

    async def scenario():
        scheduler = _scheduler(harness)
        try:
            await scheduler.start()
            completed = await _wait_record(
                scheduler, "review", lambda item: item.status == "done"
            )
            (chore,) = load_chores(layout)
            (original,) = harness.store.list_runs()
            assert (
                original.status == "succeeded" and original.thread.id == chore.thread_id
            )
            forked = None
            if operation == "fork":
                forked = harness.threads.fork(
                    thread_id=chore.thread_id, run_id=original.id
                )
                assert forked != chore.thread_id
                assert ThreadRef(forked).kind == ThreadKind.CHORE
            else:
                harness.threads.rewind(thread_id=chore.thread_id, run_id=original.id)

            checkpoint = ChoreStore(layout.run_store, read_only=True)
            try:
                assert checkpoint.list() == (completed,)
            finally:
                checkpoint.close()
            assert load_chores(layout) == (chore,)

            await scheduler.stop()
            scheduler = _scheduler(harness)
            await scheduler.start()
            await scheduler.refresh()
            assert await scheduler.get("review") == completed
            assert len(harness.adapter.invocations) == 1
            assert len(harness.store.list_runs()) == 1

            await scheduler.run_chore(chore.id)
            await _wait_record(scheduler, "review", lambda item: item.status == "done")
            assert len(harness.adapter.invocations) == 2
            roots = harness.store.list_runs()
            assert len(roots) == 2
            assert all(run.thread.id == chore.thread_id for run in roots)
            if forked is not None:
                assert [
                    run.id
                    for run in harness.store.list_thread_history_chronological(
                        thread_id=forked
                    )
                ] == [original.id]
            else:
                visible = harness.store.list_thread_history_chronological(
                    thread_id=chore.thread_id
                )
                assert len(visible) == 1 and visible[0].id != original.id
        finally:
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_busy_chore_thread_keeps_its_source_trigger_until_admission(
    tmp_path, monkeypatch
):
    from toolang.execution.errors import ThreadBusy

    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        prepare_state=True,
        source=_SOURCE,
        responses=[ScriptedModelTurn(_result("occupant"), gate=gate), _result("chore")],
    )
    catalog = AuthoredChores(
        harness.setup.layout.home,
        lock_path=harness.setup.layout.home_edit_lock,
        agent_name=harness.setup.layout.name,
    )
    _create_chore(catalog, "review", "/agic:review focus=chore\n\nKeep queued.")
    scheduler = _scheduler(harness)
    thread = f"chore_{chore_id('review', agent_name=harness.setup.layout.name)}"
    harness.store.create_thread(thread_id=thread)
    rejected = threading.Event()
    run = harness.executor.run

    def observed_run(*args, **kwargs):
        try:
            return run(*args, **kwargs)
        except ThreadBusy:
            rejected.set()
            raise

    monkeypatch.setattr(harness.executor, "run", observed_run)

    async def scenario():
        try:
            occupant = harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:review",
                    primary=(TextPart("Independent work"),),
                    named={"focus": "occupant"},
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            await scheduler.start()
            assert await asyncio.to_thread(rejected.wait, 2)
            deferred = await _wait_record(
                scheduler, "review", lambda item: item.active_run is not None
            )
            assert deferred.active_run != occupant.run_id
            assert deferred.active_trigger == "source" and deferred.error is None
            assert len(harness.store.list_runs()) == 1
            gate.release()
            assert (await occupant).status == "succeeded"
            finished = await _wait_record(
                scheduler, "review", lambda item: item.status == "done"
            )
            assert finished.error is None and finished.active_run is None
            assert (
                len(harness.store.list_runs()) == len(harness.adapter.invocations) == 2
            )
        finally:
            gate.release()
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_scheduler_rejects_invalid_input_without_creating_a_run(
    tmp_path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        prepare_state=True,
        source=_SOURCE,
        responses=[],
    )
    _create_chore(
        AuthoredChores(
            harness.setup.layout.home,
            lock_path=harness.setup.layout.home_edit_lock,
            agent_name=harness.setup.layout.name,
        ),
        "review",
        ":help",
    )
    scheduler = _scheduler(harness)

    async def scenario() -> None:
        try:
            await scheduler.start()
            record = await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "failed",
            )

            assert record.active_run is None
            assert record.error is not None
            assert "colon commands cannot be used in a run submission" in record.error
            assert not harness.store.list_runs(limit=None)
            assert not harness.store.list_threads()
        finally:
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_invalid_definition_startup_closes_chore_store(tmp_path, monkeypatch) -> None:
    from toolang.driver.chore import scheduler as scheduler_module

    harness = ExecutionHarness.create(
        tmp_path / "toolang", prepare_state=True, source=_SOURCE, responses=[]
    )
    directory = harness.setup.layout.home / "chores"
    directory.mkdir()
    (directory / "broken.md").write_text("---\nschedule: invalid\n---\nReview.\n")
    closed = []

    class TrackedStore(ChoreStore):
        def close(self) -> None:
            super().close()
            closed.append(self.db_path)

    monkeypatch.setattr(scheduler_module, "ChoreStore", TrackedStore)
    scheduler = _scheduler(harness)

    async def scenario() -> None:
        try:
            await scheduler.start()
            assert await scheduler.get("broken") is None
            assert not harness.store.list_runs(limit=None)
        finally:
            await _close_scheduler(scheduler, harness)
        assert closed == [harness.setup.layout.run_store]

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_scheduler_submits_and_awaits_runs_on_the_execution_loop(
    tmp_path,
    monkeypatch,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        prepare_state=True,
        source=_SOURCE,
        responses=[_result()],
    )
    _create_chore(
        AuthoredChores(
            harness.setup.layout.home,
            lock_path=harness.setup.layout.home_edit_lock,
            agent_name=harness.setup.layout.name,
        ),
        "review",
        "/agic:review focus=security\n\nReview this.",
    )
    scheduler = _scheduler(harness)
    run_threads: list[int] = []
    original_run = harness.executor.run

    def record_run(*args, **kwargs):
        run_threads.append(threading.get_ident())
        return original_run(*args, **kwargs)

    monkeypatch.setattr(harness.executor, "run", record_run)

    async def scenario() -> None:
        execution_thread = threading.get_ident()
        try:
            await scheduler.start()
            record = await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "done",
            )

            assert record.active_run is None
            assert scheduler._thread is not None
            assert scheduler._thread.ident != execution_thread
            assert run_threads == [execution_thread]

            runs = harness.store.list_runs(limit=None)
            assert len(runs) == 1
            assert (
                str(runs[0].thread)
                == f"chore_{chore_id('review', agent_name=harness.setup.layout.name)}"
            )
            control = harness.store.get_run_control(run_id=runs[0].id, index=0)
            assert control is not None
            assert isinstance(control.payload, RunControlPayload)
            assert control.payload.runnable == "agic:review"
            assert control.payload.input == CallInput(
                {
                    "_": Array("Part[]", Message.user("Review this.").parts),
                    "focus": "security",
                }
            )
            created = harness.store.get_thread_control(
                thread_id=f"chore_{chore_id('review', agent_name=harness.setup.layout.name)}",
                index=0,
            )
            assert created is not None
            assert created.payload == CreateControlPayload()
        finally:
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_chore_runnable_call_input_expands_a_prompt_call(
    tmp_path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        prepare_state=True,
        source="""
prompt wrap:
  <{{_}}>

agic review(_: Part[], focus: Text):
  recall = none
  context = none
  instruct = none
  user: {{focus}} {{_}}
""",
        responses=[],
    )
    _create_chore(
        AuthoredChores(
            harness.setup.layout.home,
            lock_path=harness.setup.layout.home_edit_lock,
            agent_name=harness.setup.layout.name,
        ),
        "nested-input",
        "/agic:review focus=nested -\nBefore\n$wrap ---\ntarget\n---\nAfter\n",
    )
    scheduler = _scheduler(harness)

    try:
        (chore,) = load_chores(harness.setup.layout)
        spec = scheduler._build_spec(chore)

        assert spec.input["_"] == Array(
            "Part[]",
            (TextPart("Before\n<target\n>\nAfter"),),
        )
        assert spec.input["focus"] == "nested"
        assert [invocation.name for invocation in spec.prompt_invocations] == ["wrap"]
    finally:
        harness.store.close()


def test_scheduler_recovers_a_claim_with_no_accepted_run(tmp_path) -> None:
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        prepare_state=True,
        source=_SOURCE,
        responses=[_result()],
    )
    _create_chore(
        AuthoredChores(
            harness.setup.layout.home,
            lock_path=harness.setup.layout.home_edit_lock,
            agent_name=harness.setup.layout.name,
        ),
        "review",
        "/agic:review focus=recovery\n\nReview after recovery.",
    )
    (chore,) = load_chores(harness.setup.layout)
    checkpoint = ChoreStore(harness.setup.layout.chore_store)
    try:
        checkpoint.reconcile(
            chores={chore.id: chore},
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        checkpoint.claim(
            chore=chore,
            trigger="source",
            run_id="run_missing",
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    finally:
        checkpoint.close()
    scheduler = _scheduler(harness)

    async def scenario() -> None:
        try:
            await scheduler.start()
            await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "done",
            )

            runs = harness.store.list_runs(limit=None)
            assert len(runs) == 1
            assert runs[0].id != "run_missing"
        finally:
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_scheduler_accepts_blocking_controls_from_api_worker_threads(
    tmp_path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        prepare_state=True,
        source=_SOURCE,
        responses=[_result("first"), _result("reopened")],
    )
    _create_chore(
        AuthoredChores(
            harness.setup.layout.home,
            lock_path=harness.setup.layout.home_edit_lock,
            agent_name=harness.setup.layout.name,
        ),
        "review",
        "/agic:review focus=worker\n\nReview from a worker.",
    )
    scheduler = _scheduler(harness)

    async def scenario() -> None:
        try:
            await scheduler.start()
            await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "done",
            )

            reopened = await asyncio.to_thread(
                scheduler.run_chore_sync,
                "review",
            )
            assert reopened.status == "pending"
            await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "done",
            )
            assert len(harness.store.list_runs(limit=None)) == 2
        finally:
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_scheduler_serializes_body_changes(tmp_path) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        prepare_state=True,
        source=_SOURCE,
        responses=[
            ScriptedModelTurn(result=_result("first"), gate=gate),
            _result("second"),
        ],
    )
    catalog = AuthoredChores(
        harness.setup.layout.home,
        lock_path=harness.setup.layout.home_edit_lock,
        agent_name=harness.setup.layout.name,
    )
    authored = _create_chore(
        catalog,
        "review",
        "/agic:review focus=first\n\nReview the first revision.",
    )
    scheduler = _scheduler(harness)

    async def scenario() -> None:
        try:
            await scheduler.start()
            await gate.wait_until_entered()
            running = await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "running",
            )
            first_run = running.active_run
            assert first_run is not None

            catalog.update(
                authored.with_body(
                    "/agic:review focus=second\n\nReview the second revision."
                )
            )
            await scheduler.refresh()
            edited = await scheduler.get("review")
            assert edited is not None
            assert edited.status == "running"
            assert edited.active_run == first_run
            assert len(harness.store.list_runs(limit=None)) == 1

            gate.release()
            await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "done",
            )
            async with asyncio.timeout(5):
                while len(harness.store.list_runs(limit=None)) != 2:
                    await asyncio.sleep(0.01)

            runs = harness.store.list_runs(limit=None)
            assert {str(run.thread) for run in runs} == {
                f"chore_{chore_id('review', agent_name=harness.setup.layout.name)}"
            }
            assert len(harness.adapter.invocations) == 2
        finally:
            gate.release()
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_scheduler_allows_different_chores_to_run_concurrently(tmp_path) -> None:
    first_gate = AsyncGate()
    second_gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        prepare_state=True,
        source=_SOURCE,
        responses=[
            ScriptedModelTurn(result=_result("first"), gate=first_gate),
            ScriptedModelTurn(result=_result("second"), gate=second_gate),
        ],
    )
    catalog = AuthoredChores(
        harness.setup.layout.home,
        lock_path=harness.setup.layout.home_edit_lock,
        agent_name=harness.setup.layout.name,
    )
    _create_chore(catalog, "alpha", "/agic:review focus=alpha\n\nAlpha.")
    _create_chore(catalog, "beta", "/agic:review focus=beta\n\nBeta.")
    scheduler = _scheduler(harness)

    async def scenario() -> None:
        try:
            await scheduler.start()
            await asyncio.gather(
                first_gate.wait_until_entered(),
                second_gate.wait_until_entered(),
            )
            alpha, beta = await asyncio.gather(
                scheduler.get("alpha"),
                scheduler.get("beta"),
            )

            assert alpha is not None and alpha.status == "running"
            assert beta is not None and beta.status == "running"
            assert alpha.active_run != beta.active_run
            assert len(harness.adapter.invocations) == 2

            first_gate.release()
            second_gate.release()
            await asyncio.gather(
                _wait_record(
                    scheduler,
                    "alpha",
                    lambda item: item.status == "done",
                ),
                _wait_record(
                    scheduler,
                    "beta",
                    lambda item: item.status == "done",
                ),
            )
        finally:
            first_gate.release()
            second_gate.release()
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


@pytest.mark.parametrize("runnable_kind", ["agic", "flow"])
@pytest.mark.parametrize("specialized", [False, True])
def test_chore_selects_named_handler_before_unnamed_entry(
    tmp_path, runnable_kind, specialized
):
    source = f"{runnable_kind}:\n  pass\n"
    if specialized:
        source += f"\n{runnable_kind} chore:\n  pass\n"
    harness = ExecutionHarness.create(
        tmp_path, prepare_state=True, source=source, responses=[]
    )
    _create_chore(
        AuthoredChores(
            harness.setup.layout.home,
            lock_path=harness.setup.layout.home_edit_lock,
            agent_name=harness.setup.layout.name,
        ),
        "entry",
        "Handle this request.",
    )
    try:
        (chore,) = load_chores(harness.setup.layout)
        spec = _scheduler(harness)._build_spec(chore)
        entry = next(
            name for name in harness.state.runnables if name.startswith("<entry:")
        )
        expected = "chore" if specialized else entry
        assert spec.bindings.runnable in {
            f"{runnable_kind}:{expected}",
            f"flow:{expected}",
        }
    finally:
        harness.store.close()


def test_chore_without_handler_or_entry_fails(tmp_path) -> None:
    harness = ExecutionHarness.create(
        tmp_path, prepare_state=True, source="agic review:\n  pass\n", responses=[]
    )
    _create_chore(
        AuthoredChores(
            harness.setup.layout.home,
            lock_path=harness.setup.layout.home_edit_lock,
            agent_name=harness.setup.layout.name,
        ),
        "entry",
        "Handle this request.",
    )
    try:
        (chore,) = load_chores(harness.setup.layout)
        with pytest.raises(ToolangError, match="or unnamed entry"):
            _scheduler(harness)._build_spec(chore)
    finally:
        harness.store.close()


@pytest.mark.parametrize("specialized", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_chore_handler_selects_invocation_named_or_unnamed_entry(
    tmp_path, specialized, explicit
):
    source = "agic:\n  {{_}}\nagic other:\n  {{_}}\n"
    if specialized:
        source += "agic chore:\n  {{_}}\n"
    harness = ExecutionHarness.create(
        tmp_path, prepare_state=True, source=source, responses=[]
    )
    setup = replace(
        harness.setup,
        defaults=replace(harness.setup.defaults),
    )
    _create_chore(
        AuthoredChores(
            setup.layout.home,
            lock_path=setup.layout.home_edit_lock,
            agent_name=setup.layout.name,
        ),
        "entry",
        ("/agic:other\n" if explicit else "") + "Handle this request.",
    )
    try:
        (chore,) = load_chores(setup.layout)
        spec = _scheduler(harness)._build_spec(chore, setup=setup)
        expected = (
            "other"
            if explicit
            else "chore"
            if specialized
            else next(
                name for name in harness.state.runnables if name.startswith("<entry:")
            )
        )
        assert spec.bindings.runnable == f"agic:{expected}"
    finally:
        harness.store.close()


def test_driver_consumes_published_state_without_reading_authored_chores(
    tmp_path, monkeypatch
):
    harness = ExecutionHarness.create(
        tmp_path, source=_SOURCE, responses=[_result()], prepare_state=True
    )
    file = _create_chore(
        AuthoredChores(
            harness.setup.layout.home,
            lock_path=harness.setup.layout.home_edit_lock,
            agent_name=harness.setup.layout.name,
        ),
        "review",
        "/agic:review focus=published\nReview.",
    )
    from toolang.state.prepare import prepare_agent_state

    published = prepare_agent_state(harness.setup.layout)
    driver = ChoreDriver(
        layout=harness.setup.layout,
        executor=harness.executor,
        ids=harness.ids,
        get_agent_setup=lambda: harness.setup,
        get_agent_state=lambda: published,
    )
    monkeypatch.setattr(
        AuthoredChores,
        "list",
        lambda *_: pytest.fail("driver must not read authored files"),
    )
    assert file.path is not None
    file.path.unlink()

    async def scenario():
        try:
            await driver.start()
            await _wait_record(driver, "review", lambda record: record.status == "done")
            assert len(harness.store.list_runs(limit=None)) == 1
        finally:
            await _close_scheduler(driver, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_invalid_edit_preserves_active_run_and_manual_cancellation(tmp_path):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_SOURCE,
        responses=[ScriptedModelTurn(result=_result(), gate=gate)],
        prepare_state=True,
    )
    catalog = AuthoredChores(
        harness.setup.layout.home,
        lock_path=harness.setup.layout.home_edit_lock,
        agent_name=harness.setup.layout.name,
    )
    original = _create_chore(catalog, "review", "/agic:review focus=active\nReview.")
    driver = _scheduler(harness)

    async def scenario():
        try:
            await driver.start()
            await gate.wait_until_entered()
            before = await _wait_record(
                driver, "review", lambda record: record.status == "running"
            )
            assert original.path is not None
            assert before.active_run is not None
            original.path.write_text("---\nschedule: invalid\n---\nRejected.")
            await driver.refresh()
            assert driver.get_agent_state().chores == ()
            current = await driver.get("review")
            assert current is not None
            assert current.active_run == before.active_run
            with pytest.raises(FileNotFoundError):
                await driver.run_chore("review")
            canceled = await driver.cancel_chore("review")
            assert canceled.active_run == before.active_run
            harness.executor.cancel(
                run_id=before.active_run, reason="Manual cancellation"
            )
            gate.release()
            terminal = await _wait_record(
                driver, "review", lambda record: record.status == "canceled"
            )
            assert terminal.body_hash == before.body_hash
            assert len(harness.store.list_runs(limit=None)) == 1
        finally:
            gate.release()
            await _close_scheduler(driver, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()
