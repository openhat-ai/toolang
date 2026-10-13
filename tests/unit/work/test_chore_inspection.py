"""Current authored definitions and durable checkpoints remain distinguishable."""

from datetime import datetime, timezone

from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.common.layout import AgentLayout
from toolang.driver.chore.inspection import ChoreInspection
from toolang.catalog.chores import load_chores
from toolang.driver.chore.store import ChoreStore


def test_inspection_shows_pending_time_and_detects_unprocessed_edits(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    catalog = AuthoredChores(layout.home)
    original = catalog.create(ChoreFile.parse("Original body.", name="once"))
    chore = load_chores(layout)[0]
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    store = ChoreStore(layout.run_store)
    try:
        store.reconcile(chores=[chore], now=now)
        info = ChoreInspection.load(layout=layout, runs=()).list()[0]
        assert info.runtime.synced
        assert info.runtime.pending_at == now.isoformat()
        assert info.runtime.next_scheduled_at is None
        store.claim(chore=chore, trigger="source", run_id="run_once", now=now)
        store.finish_run(
            chores={chore.id: chore}, run_id="run_once", run_status="succeeded", now=now
        )
        catalog.update(original.with_body("New body."))
        inspection = ChoreInspection.load(layout=layout, runs=())
        entry = inspection.get("once")
        assert entry is not None
        detail = inspection.detail(entry)
        assert detail.body == "New body."
        assert detail.runtime.status == "done"
        assert not detail.runtime.synced
        assert detail.runtime.updated_at == now.isoformat()
    finally:
        store.close()


def test_inline_inspection_is_read_only_without_state_or_database(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("chore once:\n  Do work.\nflow broken:\n  run missing\n")
    info = ChoreInspection.load(layout=layout, runs=()).list()[0]
    assert info.path == "agent.too" and info.line == 1
    assert info.runtime.status is None and info.runtime.updated_at is None
    assert not info.runtime.synced
    assert not layout.run_store.exists()
    assert not layout.agent_state.exists()
