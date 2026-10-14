"""Current authored definitions and durable checkpoints remain distinguishable."""

from datetime import datetime, timezone

from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.common.layout import AgentLayout
from toolang.driver.chore.inspection import ChoreInspection
from toolang.catalog.chores import load_chores
from toolang.driver.chore.store import ChoreStore


def test_inspection_shows_pending_time_and_detects_unprocessed_edits(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    catalog = AuthoredChores(layout.home, lock_path=layout.home_edit_lock)
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


def test_invalid_chore_name_is_inspected_by_source_without_execution_identity(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    directory = layout.home / "chores"
    directory.mkdir(parents=True)
    (directory / " bad.md").write_text("Invalid name, readable source.")
    (directory / "good.md").write_text("Valid sibling.")

    inspection = ChoreInspection.load(layout=layout, runs=())
    entries = {entry.path: entry for entry in inspection.list()}
    invalid = entries["chores/ bad.md"]
    assert invalid.source_error
    assert invalid.id is None
    assert invalid.ref == "chores/ bad.md"
    assert invalid.runtime.thread_id is None
    assert invalid.runtime.status is None
    assert entries["chores/good.md"].source_error is None
    source = inspection.get(invalid.ref)
    assert source is not None
    assert inspection.detail(source).body == "Invalid name, readable source."
    catalog = AuthoredChores(layout.home, lock_path=layout.home_edit_lock)
    assert catalog.raw_path("good") == directory / "good.md"
    assert catalog.remove_file(invalid.ref) == directory / " bad.md"
    assert catalog.remove_file("good") == directory / "good.md"


def test_invalid_agent_source_does_not_block_markdown_inspection_or_authoring(tmp_path):
    from toolang.catalog.chore_authoring import create_chore, editable_chore_path

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("flow (")
    catalog = AuthoredChores(layout.home, lock_path=layout.home_edit_lock)
    catalog.create(ChoreFile.parse("Valid home definition.", name="good"))

    inspection = ChoreInspection.load(layout=layout, runs=())
    assert [entry.name for entry in inspection.list()] == ["good"]
    assert not layout.agent_state.exists()
    created = create_chore(layout, ChoreFile.parse("New home definition.", name="new"))
    assert created.path is not None and created.path.is_file()
    path = editable_chore_path(layout, "chores/good.md")
    assert path.read_text() == "Valid home definition."
    catalog.remove_file("good")
    assert not path.exists()
