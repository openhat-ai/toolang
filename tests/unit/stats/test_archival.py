from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier
from unittest.mock import Mock

import pytest

from toolang.stats import _sql as sql, records
from toolang.stats.types import Scope

from .conftest import DAY, NOW


def dump(store):
    with store.writer() as connection:
        return tuple(
            connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in ("stats_roots", "stats_buckets")
        )


def test_successful_roots_are_collected_once_across_refreshes_and_restart(
    fixture, monkeypatch
):
    root = fixture.root("old", NOW - 4 * DAY)
    fixture.call(root, NOW - 3 * DAY, NOW - 3 * DAY + 1)
    fixture.finish(root, NOW - 2 * DAY)
    collect = Mock(wraps=fixture.stats.records.collect)
    monkeypatch.setattr(fixture.stats.records, "collect", collect)
    before = fixture.stats.summary().data
    assert fixture.refresh() is None
    expected = dump(fixture.stats.records)
    for _ in range(3):
        assert fixture.refresh() is None
        assert fixture.stats.summary().data == before
    fixture.stats.records.initialize()
    assert fixture.refresh() is None
    assert dump(fixture.stats.records) == expected
    assert collect.call_count == 1


def test_stale_inventory_skips_collection_before_reading_steps(fixture, monkeypatch):
    root = fixture.root("stale", NOW - 4 * DAY)
    fixture.call(root, NOW - 3 * DAY, NOW - 3 * DAY + 1)
    fixture.finish(root, NOW - 2 * DAY)
    store = fixture.stats.records
    with store.collect(root, lambda: False) as additions:
        original = store.snapshot
        inventory = True

        @contextmanager
        def snapshot():
            nonlocal inventory
            with original() as reader:
                yield reader
            if inventory:
                inventory = False
                assert store.publish(root, NOW - 2 * DAY, NOW, additions)

        monkeypatch.setattr(store, "snapshot", snapshot)
        collect = Mock(side_effect=AssertionError("archived root was recomputed"))
        monkeypatch.setattr(store, "collect", collect)
        assert fixture.refresh() is None
        collect.assert_not_called()


@pytest.mark.parametrize("failure", [sql.INSERT_RECEIPT, sql.TOUCH_STATE])
def test_publication_rolls_back_buckets_and_receipt_then_retries(
    fixture, monkeypatch, failure
):
    root = fixture.root("retry", NOW - 4 * DAY)
    fixture.call(root, NOW - 3 * DAY, NOW - 3 * DAY + 1)
    fixture.finish(root, NOW - 2 * DAY)
    store = fixture.stats.records
    before = dump(store)
    original = records.connect

    def fail_connect(path, **kwargs):
        connection = original(path, **kwargs)
        if not kwargs.get("read_only"):

            def authorize(action, table, *_):
                failed = (
                    failure == sql.INSERT_RECEIPT
                    and action == sqlite3.SQLITE_INSERT
                    and table == "stats_roots"
                    or failure == sql.TOUCH_STATE
                    and action == sqlite3.SQLITE_UPDATE
                    and table == "stats_state"
                )
                return sqlite3.SQLITE_DENY if failed else sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
        return connection

    with store.collect(root, lambda: False) as additions:
        with monkeypatch.context() as patch:
            patch.setattr(records, "connect", fail_connect)
            with pytest.raises(sqlite3.DatabaseError):
                store.publish(root, NOW - 2 * DAY, NOW, additions)
        assert dump(store) == before
        assert store.publish(root, NOW - 2 * DAY, NOW, additions)
        committed = dump(store)
        # A caller that lost the commit acknowledgement can retry the same batch.
        assert not store.publish(root, NOW - 2 * DAY, NOW, additions)
        assert dump(store) == committed


def test_weeks_long_root_is_discovered_after_completion_and_waiting_boundary(
    fixture, monkeypatch
):
    root = fixture.root("long", NOW - 40 * DAY)
    fixture.call(root, NOW - 25 * DAY, NOW - 25 * DAY + 1)
    fixture.call(root, NOW - 35 * DAY, NOW - 35 * DAY + 1, tool=True)
    scope = Scope("root_run", root)
    before = fixture.stats.summary(scope).data
    assert before is not None
    assert before[0].root_runs == 1 and before[0].tool_calls == 1
    assert before[2].root_runs == 0 and before[2].model_calls == 1
    collect = Mock(wraps=fixture.stats.records.collect)
    monkeypatch.setattr(fixture.stats.records, "collect", collect)
    assert fixture.refresh() is None
    fixture.finish(root, NOW)
    for cutoff in (NOW - DAY, NOW - 1, NOW):
        assert fixture.refresh(cutoff) is None
        collect.assert_not_called()
    assert fixture.refresh(NOW + 1) is None
    assert fixture.stats.summary(scope).data == before
    for _ in range(3):
        assert fixture.refresh(NOW + 1) is None
    assert collect.call_count == 1


def test_zero_call_roots_get_receipts_and_open_trees_stay_in_source(fixture):
    empty = fixture.root("empty", NOW - 4 * DAY)
    fixture.finish(empty, NOW - 2 * DAY)
    invalid = fixture.root("invalid", NOW - 4 * DAY)
    ref = fixture.call(invalid, NOW - 3 * DAY, NOW - 3 * DAY + 1, tool=True)
    fixture.root("child", NOW - 3 * DAY, parent=ref, thread="chat_stats")
    fixture.finish(invalid, NOW - 2 * DAY)
    assert "not terminal" in fixture.refresh()
    with fixture.stats.records.snapshot() as connection:
        assert connection.execute(sql.RECEIPT_EXISTS, {"root_id": empty}).fetchone()
        assert not connection.execute(
            sql.RECEIPT_EXISTS, {"root_id": invalid}
        ).fetchone()
    assert fixture.stats.summary(Scope("root_run", empty)).data[0].root_runs == 1


def test_concurrent_publishers_commit_a_root_once(fixture):
    root = fixture.root("parallel", NOW - 4 * DAY)
    fixture.call(root, NOW - 3 * DAY, NOW - 3 * DAY + 1)
    fixture.finish(root, NOW - 2 * DAY)
    before = fixture.stats.summary().data
    barrier = Barrier(2)

    def publish():
        with fixture.stats.records.collect(root, lambda: False) as additions:
            barrier.wait(timeout=10)
            return fixture.stats.records.publish(root, NOW - 2 * DAY, NOW, additions)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish) for _ in range(2)]
        assert sorted(future.result() for future in futures) == [False, True]
    assert fixture.stats.summary().data == before


def test_completion_committed_with_an_old_timestamp_is_not_skipped(fixture):
    root = fixture.root("old_completion", NOW - 40 * DAY)
    fixture.call(root, NOW - 30 * DAY, NOW - 30 * DAY + 1)
    assert fixture.refresh() is None
    fixture.finish(root, NOW - 3 * DAY)
    before = fixture.stats.summary().data
    assert fixture.refresh() is None
    with fixture.stats.records.snapshot() as connection:
        assert connection.execute(sql.RECEIPT_EXISTS, {"root_id": root}).fetchone()
    assert fixture.stats.summary().data == before


def test_large_root_pages_are_complete_and_not_revisited_after_archival(
    fixture, monkeypatch
):
    root = fixture.root("paged", NOW - 4 * DAY)
    for index in range(501):
        fixture.call(root, NOW - 3 * DAY + index, NOW - 3 * DAY + index + 1, tool=True)
    fixture.finish(root, NOW - 2 * DAY)
    before = fixture.stats.summary().data
    assert before[0].tool_calls == 501
    pages = Mock(wraps=fixture.stats.records.step_page)
    monkeypatch.setattr(fixture.stats.records, "step_page", pages)
    assert fixture.refresh() is None
    assert pages.call_count == 3
    assert fixture.stats.summary().data == before
    assert fixture.refresh() is None
    assert pages.call_count == 3


def test_canceled_root_without_start_has_receipt_but_no_contribution(fixture):
    from tests.support.execution_fixtures import accept_run
    from toolang.base.types.message import Message
    from toolang.execution.types import RunNoted
    from toolang.stats.types import timestamp

    fixture.source.create_thread(thread_id="chat_pre_entry", origin="chat")
    root = "run_pre_entry"
    accept_run(
        fixture.source,
        run_id=root,
        parent=None,
        thread="chat_pre_entry",
        input=Message.user("fixture"),
        context={},
        request_id=None,
        created_at=timestamp(NOW - 4 * DAY),
    )
    fixture.source.finish_run(
        run_id=root,
        status="canceled",
        noted=RunNoted(),
        finished_at=timestamp(NOW - 2 * DAY),
    )
    assert fixture.refresh() is None
    with fixture.stats.records.snapshot() as connection:
        assert connection.execute(sql.RECEIPT_EXISTS, {"root_id": root}).fetchone()
        assert not connection.execute(
            "SELECT 1 FROM stats_buckets WHERE scope_kind='root_run' AND scope_id=?",
            (root,),
        ).fetchone()
    result = fixture.stats.summary(Scope("root_run", root)).data[0]
    assert result.root_runs == 0 and result.model_calls == 0 and result.in_ == 0
