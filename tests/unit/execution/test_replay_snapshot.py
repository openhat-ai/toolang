import sqlite3
import pytest

from toolang.base.types.message import Message, TextPart
from toolang.execution.replay import replay_request_id, snapshot_history
from toolang.execution.store import RunStore
from toolang.common.ids import IdIssuer
from toolang.stats.recent import recent_source
from tests.support.execution_fixtures import project_run_end, project_run_start


def test_imports_are_readable_but_not_destination_activity(tmp_path):
    source = tmp_path / "source.db"
    store = RunStore(source)
    ids = IdIssuer(source, agent_name="aid")
    run = project_run_start(
        store,
        run_id=ids.issue_run(),
        thread_id=ids.issue_thread("exec"),
        input=Message(role="user", parts=(TextPart(text="hello"),)),
    )
    destination = tmp_path / "destination.db"
    snapshot_history(
        source,
        destination,
        source_owner="aid@r1",
        source_root=tmp_path,
        request_id="replay1",
    )
    copied = RunStore(destination)
    assert copied.get_run(run_id=run.id) == store.get_run(run_id=run.id)
    assert copied.is_imported(run.id)
    with recent_source(destination) as recent:
        recent.pin()
        assert not recent.has_unfinished_roots()
    next_destination = tmp_path / "next.db"
    snapshot_history(
        destination,
        next_destination,
        source_owner="aid@r2",
        source_root=tmp_path,
        request_id="replay2",
    )
    with sqlite3.connect(next_destination) as connection:
        assert (
            connection.execute(
                "SELECT owner FROM imported_runs WHERE run=?", (run.id,)
            ).fetchone()[0]
            == "aid@r1"
        )
    copied.close()
    store.close()


def test_imported_active_rows_do_not_hide_a_new_busy_owner(tmp_path):
    from toolang.execution.errors import ThreadBusy

    source = tmp_path / "source.db"
    store = RunStore(source)
    ids = IdIssuer(source, agent_name="aid")
    thread = ids.issue_thread("exec")
    project_run_start(
        store, run_id=ids.issue_run(), thread_id=thread, input=Message.user("old")
    )
    destination = tmp_path / "next.db"
    snapshot_history(
        source,
        destination,
        source_owner="old",
        source_root=tmp_path,
        request_id="replay",
    )
    copied = RunStore(destination)
    try:
        copied.require_idle_thread(thread)
        ids = IdIssuer(destination, agent_name="aid")
        project_run_start(
            copied, run_id=ids.issue_run(), thread_id=thread, input=Message.user("new")
        )
        with pytest.raises(ThreadBusy):
            copied.require_idle_thread(thread)
    finally:
        copied.close()
        store.close()


def test_schema_62_read_only_and_migration_preserve_facts(tmp_path):
    path = tmp_path / "runs.db"
    store = RunStore(path)
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE imported_runs")
        connection.execute("DROP TABLE replay_provenance")
        connection.execute("PRAGMA user_version=62")
    readonly = RunStore(path, read_only=True)
    assert not readonly.is_imported("anything")
    readonly.close()
    assert replay_request_id(path, "run_absent") is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 62
    migrated = RunStore(path)
    migrated.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 63


def test_selected_snapshot_requires_terminal_idle_history_and_records_acceptance(
    tmp_path,
):
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    store = RunStore(source)
    ids = IdIssuer(source, agent_name="aid")
    thread = ids.issue_thread("chat")
    run = project_run_start(
        store, run_id=ids.issue_run(), thread_id=thread, input=Message.user("old")
    )
    with pytest.raises(ValueError, match="not terminal"):
        snapshot_history(
            source,
            destination,
            source_owner="old",
            source_root=tmp_path,
            request_id="seed",
            source_run=run.id,
        )
    assert not destination.exists()
    assert not list(tmp_path.glob(".seed-*"))
    project_run_end(store, run_id=run.id)
    head = str(store.thread_view(str(thread)).head)
    snapshot_history(
        source,
        destination,
        source_owner="old",
        source_root=tmp_path,
        request_id="seed",
        source_thread="latest",
    )
    store.close()
    copied = RunStore(destination)
    try:
        copied_ids = IdIssuer(destination, agent_name="aid")
        accepted = project_run_start(
            copied,
            run_id=copied_ids.issue_run(),
            thread_id=thread,
            input=Message.user("new"),
            request_id="actual-chat-request",
        )
        receipt = copied._conn.execute(
            "SELECT request_id,source_root,source_thread,source_head,destination_run FROM replay_provenance"
        ).fetchall()
        assert [tuple(row) for row in receipt] == [
            ("actual-chat-request", str(tmp_path), str(thread), head, accepted.id)
        ]
    finally:
        copied.close()


def test_imported_failed_talk_receipts_cannot_send_failure_notices(tmp_path):
    from toolang.execution.talk import TalkBatch
    from toolang.execution.types import ConversationScope

    source = tmp_path / "source.db"
    store = RunStore(source)
    ids = IdIssuer(source, agent_name="aid")
    thread = ids.issue_thread("talk")
    scope = ConversationScope("test", "conversation")
    store.talk.bind(scope, str(thread))
    run = project_run_start(
        store, run_id=ids.issue_run(), thread_id=thread, input=Message.user("old")
    )
    with store.write_transaction():
        store.talk.accept(TalkBatch(scope, "0-0", "1-0", "{}"), run.id, str(thread))
    project_run_end(store, run_id=run.id, status="failed")
    destination = tmp_path / "destination.db"
    snapshot_history(
        source,
        destination,
        source_owner="old",
        source_root=tmp_path,
        request_id="seed",
        source_run=run.id,
    )
    copied = RunStore(destination)
    try:
        assert not copied.talk.attempt_notice(run.id)
        receipt = copied.talk.get(scope, "0-0")
        assert receipt is not None and not receipt.notice_attempted
        assert store.talk.attempt_notice(run.id)
    finally:
        copied.close()
        store.close()


def test_rerun_thread_selector_is_resolved_inside_the_snapshot(tmp_path):
    source = tmp_path / "source.db"
    store = RunStore(source)
    ids = IdIssuer(source, agent_name="aid")
    thread = ids.issue_thread("exec")
    first = project_run_start(
        store,
        run_id=ids.issue_run(),
        thread_id=thread,
        input=Message.user("first"),
        created_at="2026-10-20T00:00:00Z",
    )
    project_run_end(store, run_id=first.id)
    latest = project_run_start(
        store,
        run_id=ids.issue_run(),
        thread_id=thread,
        input=Message.user("latest"),
        created_at="2026-10-20T00:01:00Z",
    )
    project_run_end(store, run_id=latest.id)
    destination = tmp_path / "destination.db"
    snapshot_history(
        source,
        destination,
        source_owner="old",
        source_root=tmp_path,
        request_id="seed",
        source_run=str(thread),
    )
    store.close()
    with sqlite3.connect(destination) as copied:
        assert (
            copied.execute("SELECT source_run FROM replay_provenance").fetchone()[0]
            == latest.id
        )
    assert replay_request_id(destination, latest.id) == "seed"
    assert replay_request_id(destination, first.id) is None
    copied_store = RunStore(destination)
    try:
        project_run_start(
            copied_store,
            run_id=IdIssuer(destination, agent_name="aid").issue_run(),
            thread_id=thread,
            input=Message.user("replayed"),
            request_id="seed",
        )
    finally:
        copied_store.close()
    assert replay_request_id(destination, latest.id) is None


def test_partial_state_seed_cannot_be_used_as_a_replay_source(tmp_path, monkeypatch):
    from toolang.up import replay
    from toolang.up.identity import allocate_roaming, select_roaming

    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    layout = allocate_roaming(source, toolang_root=tmp_path / "global")
    state = layout.home / ".state"
    state.mkdir()
    (state / "captured").write_text("immutable state")
    store = RunStore(layout.run_store)
    ids = IdIssuer(layout.run_store, agent_name="aid")
    run = project_run_start(
        store,
        run_id=ids.issue_run(),
        thread_id=ids.issue_thread("exec"),
        input=Message.user("first"),
    )
    project_run_end(store, run_id=run.id)
    store.close()
    with monkeypatch.context() as patch:

        def fail(*_args, **_kwargs):
            raise OSError("state copy failed")

        patch.setattr(replay.shutil, "copytree", fail)
        with pytest.raises(OSError, match="state copy failed"):
            replay.prepare_continuation(
                layout, toolang_root=tmp_path / "global", source_run=run.id
            )
    incomplete = select_roaming(source)
    assert incomplete.run_store.is_file()
    from toolang.up.core import AgentCore

    with pytest.raises(ValueError, match="incomplete replay seed"):
        AgentCore(incomplete)
    with pytest.raises(ValueError, match="incomplete replay seed"):
        replay.prepare_continuation(
            incomplete, toolang_root=tmp_path / "global", source_run=run.id
        )
