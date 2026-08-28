from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from tests.support.execution_fixtures import project_run_start, project_step
from toolang.base.types.message import Message
from toolang.base.types.policy import RunLimits
from toolang.execution.errors import RunStoreSchemaError
from toolang.execution.records import CancelControlPayload, RunControlPayload
from toolang.execution.store import RunStore
from toolang.execution.types import AgentResources


def test_run_store_rejects_a_newer_schema_without_modifying_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE future_state (value TEXT NOT NULL)")
    connection.execute("INSERT INTO future_state VALUES ('preserved')")
    connection.execute("PRAGMA user_version=32")
    connection.commit()
    connection.close()

    with pytest.raises(RunStoreSchemaError) as raised:
        RunStore(path)

    assert raised.value.version == 32
    assert raised.value.current == 31
    connection = sqlite3.connect(path)
    try:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 32
        assert connection.execute("SELECT value FROM future_state").fetchone() == (
            "preserved",
        )
    finally:
        connection.close()


def test_run_store_migrates_model_continuation_state(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    RunStore(path).close()
    connection = sqlite3.connect(path)
    given = {
        "call": {
            "instructions": "instructions-ref",
            "messages": ["message-ref"],
            "state": {"cursor": "turn-1"},
            "tools": None,
        },
        "model": "test/model",
    }
    noted = {
        "accounting": None,
        "cost": None,
        "price": None,
        "state": {"cursor": "turn-2"},
        "tokens": None,
    }
    connection.execute(
        """
        INSERT INTO steps(
            run, path, kind, input, state_target, state_index,
            output, occurrence, given, noted, status,
            error, created_at, started_at, finished_at
        ) VALUES ('run-1', '0', 'model', '[]', 'run-1', 0,
                  NULL, NULL, ?, ?, 'succeeded',
                  NULL, 'now', 'now', 'now')
        """,
        (json.dumps(given), json.dumps(noted)),
    )
    connection.execute(
        """
        INSERT INTO controls(
            scope, target, "index", kind, request, status, error, timing,
            payload, created_at, finished_at, _claimed, _revision
        ) VALUES ('run', 'run-1', 0, 'preparation', NULL, 'applied', NULL,
                  'immediate', '{"state":"agent-revision"}', 'now', 'now', 1, 1)
        """
    )
    connection.execute("PRAGMA user_version=28")
    connection.commit()
    connection.close()

    RunStore(path).close()

    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT given, noted FROM steps").fetchone()
        assert row is not None
        migrated_given = json.loads(str(row[0]))
        migrated_noted = json.loads(str(row[1]))
        assert migrated_given["call"]["cont"] == {"cursor": "turn-1"}
        assert "state" not in migrated_given["call"]
        assert migrated_noted["cont"] == {"cursor": "turn-2"}
        assert "state" not in migrated_noted
        payload = connection.execute("SELECT payload FROM controls").fetchone()
        assert payload == ('{"state":"agent-revision"}',)
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 31
    finally:
        connection.close()


@pytest.mark.parametrize("schema_version", (28, 29))
def test_run_store_migrates_run_and_cancel_control_kinds(
    tmp_path: Path,
    schema_version: int,
) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)
    store.create_thread(
        thread_id="term_test",
        origin="test",
        created_at="2026-08-27T00:00:00Z",
    )
    store.accept_run(
        run_id="run_test",
        parent=None,
        thread="term_test",
        resources=AgentResources(),
        limits=RunLimits(),
        state="0" * 64,
        runnable="flow:test",
        model="none",
        locals=(),
        sandbox="host",
        occurrence=None,
        request_id="run_request",
        created_at="2026-08-27T00:00:00Z",
    )
    store.accept_run_control(
        run_id="run_test",
        kind="cancel",
        timing="immediate",
        locals=(),
        request_id="cancel_request",
        created_at="2026-08-27T00:00:01Z",
    )
    store.close()

    connection = sqlite3.connect(path)
    connection.execute("UPDATE controls SET kind = 'start' WHERE kind = 'run'")
    connection.execute("UPDATE controls SET kind = 'stop' WHERE kind = 'cancel'")
    connection.execute(f"PRAGMA user_version={schema_version}")
    connection.commit()
    connection.close()

    migrated = RunStore(path)
    try:
        controls = migrated.list_run_controls(run_id="run_test")
        assert [control.kind for control in controls] == ["run", "cancel"]
        assert isinstance(controls[0].payload, RunControlPayload)
        assert isinstance(controls[1].payload, CancelControlPayload)
    finally:
        migrated.close()

    connection = sqlite3.connect(path)
    try:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 31
    finally:
        connection.close()


def test_run_store_migrates_historical_run_and_step_state_references(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)
    root = project_run_start(
        store,
        run_id="run_state_root",
        thread_id="term_state_migration",
        origin="test",
        input=Message.user("root"),
        runnable_kind="flow",
    )
    parent = project_step(
        store,
        run_id=root.id,
        step_index=0,
        kind="run",
        status="running",
        input=(),
        output=(),
        started_at="2026-08-27T00:00:01Z",
        finished_at=None,
    )
    child = project_run_start(
        store,
        run_id="run_state_child",
        thread_id=root.thread,
        origin="test",
        input=Message.user("child"),
        parent=parent.path,
    )
    project_step(
        store,
        run_id=child.id,
        step_index=0,
        kind="value",
        status="succeeded",
        input=(),
        output=(),
        started_at="2026-08-27T00:00:02Z",
        finished_at="2026-08-27T00:00:03Z",
    )
    store.close()

    connection = sqlite3.connect(path)
    raw_payload = connection.execute(
        'SELECT payload FROM controls WHERE target = ? AND "index" = 0',
        (child.id,),
    ).fetchone()
    assert raw_payload is not None
    payload = json.loads(str(raw_payload[0]))
    payload["state"] = "0" * 64
    legacy_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    connection.execute(
        'UPDATE controls SET payload = ? WHERE target = ? AND "index" = 0',
        (legacy_payload, child.id),
    )
    for table in ("runs", "steps"):
        connection.execute(f"ALTER TABLE {table} DROP COLUMN state_index")
        connection.execute(f"ALTER TABLE {table} DROP COLUMN state_target")
    connection.execute("PRAGMA user_version=30")
    connection.commit()
    connection.close()

    migrated = RunStore(path)
    try:
        migrated_root = migrated.get_run(run_id=root.id)
        migrated_child = migrated.get_run(run_id=child.id)
        assert migrated_root is not None and migrated_child is not None
        assert migrated_root.state.target == root.id
        assert migrated_root.state.index == 0
        assert migrated_child.state.target == child.id
        assert migrated_child.state.index == 0
        assert migrated.resolve_state_revision(migrated_root.state) == "0" * 64
        assert migrated.resolve_state_revision(migrated_child.state) == "0" * 64
        assert {step.state for step in migrated.list_steps(run_id=root.id)} == {
            migrated_root.state
        }
        assert {step.state for step in migrated.list_steps(run_id=child.id)} == {
            migrated_child.state
        }
    finally:
        migrated.close()
    connection = sqlite3.connect(path)
    try:
        stored_payload = connection.execute(
            'SELECT payload FROM controls WHERE target = ? AND "index" = 0',
            (child.id,),
        ).fetchone()
        assert stored_payload == (legacy_payload,)
    finally:
        connection.close()
