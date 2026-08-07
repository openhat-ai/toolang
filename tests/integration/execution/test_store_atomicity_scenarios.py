"""Run-store transaction and durable integrity scenarios."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from toolang.base.types.message import Message
from toolang.base.types.run import ModelCall
from toolang.base.types.tool import ToolDefinition
from toolang.execution.records import RunControlRef
from toolang.execution.store import RunStore
from toolang.execution.types import StepPath


def _execute_sql(db_path: Path, sql: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(sql)
        connection.commit()
    finally:
        connection.close()


def _table_count(db_path: Path, table: str) -> int:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        connection.close()


def test_start_acceptance_rolls_back_the_run_when_control_insert_fails(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        store.create_thread(thread_id="term_atomic_start")
        _execute_sql(
            store.db_path,
            """
            CREATE TRIGGER reject_start_control
            BEFORE INSERT ON run_controls
            WHEN NEW.kind = 'start'
            BEGIN
                SELECT RAISE(ABORT, 'injected start-control failure');
            END;
            """,
        )

        with pytest.raises(ValueError):
            store.accept_start(
                run_id="run_atomic_start",
                parent=None,
                thread="term_atomic_start",
                input=Message.user("hello"),
                context={},
                request_id="atomic-start",
                created_at="2026-01-01T00:00:00Z",
            )

        assert store.get_run(run_id="run_atomic_start") is None
        assert _table_count(store.db_path, "run_controls") == 0
    finally:
        store.close()


def test_step_and_control_projection_roll_back_as_one_write_unit(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        store.create_thread(thread_id="term_atomic_event")
        store.accept_start(
            run_id="run_atomic_event",
            parent=None,
            thread="term_atomic_event",
            input=Message.user("hello"),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:00Z",
        )
        control = store.accept_run_control(
            run_id="run_atomic_event",
            kind="steer",
            timing="next_step",
            input=Message.user("updated"),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:01Z",
        )
        _execute_sql(
            store.db_path,
            f"""
            CREATE TRIGGER reject_control_finish
            BEFORE UPDATE OF status ON run_controls
            WHEN OLD.run = 'run_atomic_event' AND OLD."index" = {control.index}
            BEGIN
                SELECT RAISE(ABORT, 'injected control-finish failure');
            END;
            """,
        )

        with pytest.raises(sqlite3.IntegrityError):
            with store.write_transaction():
                store.begin_step(
                    path=StepPath("run_atomic_event", (0,)),
                    kind="system",
                    input=(RunControlRef(index=control.index),),
                    given={},
                    started_at="2026-01-01T00:00:02Z",
                )
                store.finish_run_controls(
                    run_id="run_atomic_event",
                    indexes=(control.index,),
                    finished_at="2026-01-01T00:00:02Z",
                )

        assert store.list_steps(run_id="run_atomic_event") == []
        unchanged = store.get_run_control(
            run_id="run_atomic_event",
            index=control.index,
        )
        assert unchanged is not None
        assert unchanged.status == "pending"
    finally:
        store.close()


def test_model_blobs_roll_back_when_the_model_step_cannot_be_inserted(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        _execute_sql(
            store.db_path,
            """
            CREATE TRIGGER reject_model_step
            BEFORE INSERT ON steps
            WHEN NEW.kind = 'model'
            BEGIN
                SELECT RAISE(ABORT, 'injected model-step failure');
            END;
            """,
        )
        call = ModelCall(
            instructions="stable instructions",
            messages=[Message.user("hello")],
            tools=(
                ToolDefinition(
                    name="test__lookup",
                    description="Look up one value.",
                    parameters={"type": "object"},
                ),
            ),
        )
        target = {
            "ref": "test/model",
            "provider": "test",
            "name": "model",
            "model": "model",
            "adapter": "test",
            "base_url": None,
            "scope": None,
            "tags": [],
            "options": {},
            "tools": True,
            "streaming": False,
        }

        with pytest.raises(sqlite3.IntegrityError):
            with store.write_transaction():
                given = store.capture_model_call(target=target, call=call)
                store.begin_step(
                    path=StepPath("run_atomic_model", (0,)),
                    kind="model",
                    input=(),
                    given=given,
                    started_at="2026-01-01T00:00:00Z",
                )

        assert _table_count(store.db_path, "model_texts") == 0
        assert _table_count(store.db_path, "model_messages") == 0
        assert _table_count(store.db_path, "model_toolsets") == 0
        assert _table_count(store.db_path, "steps") == 0
    finally:
        store.close()


def test_run_control_revision_only_advances_when_control_state_changes(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        store.create_thread(thread_id="term_control_revision")
        store.accept_start(
            run_id="run_control_revision",
            parent=None,
            thread="term_control_revision",
            input=Message.user("hello"),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:00Z",
        )
        baseline = store.latest_run_control_revision()
        assert store.changed_run_controls(after_revision=baseline) == (
            baseline,
            (),
        )

        steer = store.accept_run_control(
            run_id="run_control_revision",
            kind="steer",
            timing="next_step",
            input=Message.user("updated"),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:01Z",
        )
        accepted_revision, accepted = store.changed_run_controls(
            after_revision=baseline
        )
        assert accepted == (steer,)
        assert accepted_revision > baseline
        assert store.changed_run_controls(after_revision=accepted_revision) == (
            accepted_revision,
            (),
        )

        canceled = store.cancel_run_control(
            run_id=steer.run,
            index=steer.index,
            canceled_at="2026-01-01T00:00:02Z",
        )
        canceled_revision, changed = store.changed_run_controls(
            after_revision=accepted_revision
        )
        assert changed == (canceled,)
        assert changed[0].status == "canceled"
        assert canceled_revision > accepted_revision
    finally:
        store.close()


def test_run_store_adds_control_revisions_without_deleting_v18_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)
    store.create_thread(thread_id="term_v18_revision")
    store.accept_start(
        run_id="run_v18_revision",
        parent=None,
        thread="term_v18_revision",
        input=Message.user("hello"),
        context={},
        request_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    store.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX idx_run_controls_revision")
        connection.execute("ALTER TABLE run_controls DROP COLUMN revision")
        connection.execute("ALTER TABLE run_controls DROP COLUMN claimed")
        connection.execute("PRAGMA user_version=18")
        connection.commit()
    finally:
        connection.close()

    reopened = RunStore(path)
    try:
        assert reopened.get_run(run_id="run_v18_revision") is not None
        assert len(reopened.list_run_controls(run_id="run_v18_revision")) == 1
        assert reopened.latest_run_control_revision() == 0
        reopened.accept_run_control(
            run_id="run_v18_revision",
            kind="steer",
            timing="next_step",
            input=Message.user("updated"),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:01Z",
        )
        assert reopened.latest_run_control_revision() == 1
    finally:
        reopened.close()


def test_claimed_control_cannot_be_canceled_before_its_event_is_persisted(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        store.create_thread(thread_id="term_claimed_control")
        store.accept_start(
            run_id="run_claimed_control",
            parent=None,
            thread="term_claimed_control",
            input=Message.user("hello"),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:00Z",
        )
        control = store.accept_run_control(
            run_id="run_claimed_control",
            kind="steer",
            timing="next_step",
            input=Message.user("updated"),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:01Z",
        )

        assert store.claim_run_controls(
            run_id=control.run,
            indexes=(control.index,),
        ) == {control.index}
        with pytest.raises(ValueError, match="already being applied"):
            store.cancel_run_control(
                run_id=control.run,
                index=control.index,
                canceled_at="2026-01-01T00:00:02Z",
            )
        unchanged = store.get_run_control(
            run_id=control.run,
            index=control.index,
        )
        assert unchanged is not None
        assert unchanged.status == "pending"
    finally:
        store.close()
