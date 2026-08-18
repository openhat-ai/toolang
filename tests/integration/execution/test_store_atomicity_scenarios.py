"""Run-store transaction and durable integrity scenarios."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from tests.support.execution_fixtures import (
    accept_run_start,
    project_run_end,
    project_run_start,
    project_step,
)
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCall
from toolang.base.types.tool import ToolDefinition
from toolang.execution.errors import RunStoreSchemaError
from toolang.execution.records import (
    RerunControlPayload,
    RetryControlPayload,
    RunControlRef,
    StartControlPayload,
)
from toolang.execution.store import RunStore
from toolang.execution.types import Local, RunStatus, StepPath, Pointer


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


@pytest.mark.parametrize(
    ("table", "column", "invalid", "message"),
    (
        ("runs", "output", "[]", "stored run output must be an object"),
        ("runs", "placement", "[]", "stored run placement must be an object"),
        ("steps", "input", "{}", "stored step input must be an array"),
        ("steps", "output", "[]", "stored step output must be an object"),
        ("steps", "placement", "[]", "stored step placement must be an object"),
        ("steps", "given", "[]", "stored step given must be an object"),
        ("steps", "noted", "[]", "stored step noted must be an object"),
    ),
)
def test_corrupted_run_and_step_fields_are_rejected(
    tmp_path: Path,
    table: str,
    column: str,
    invalid: str,
    message: str,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_corrupted_record",
            thread_id="term_corrupted_record",
            origin="chat",
            input=Message.user("hello"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        target = (
            "id = 'run_corrupted_record'"
            if table == "runs"
            else "run = 'run_corrupted_record'"
        )
        _execute_sql(
            store.db_path,
            f"UPDATE {table} SET {column} = '{invalid}' WHERE {target}",
        )

        with pytest.raises(ValueError, match=message):
            if table == "runs":
                store.get_run(run_id=run.id)
            else:
                store.list_steps(run_id=run.id)
    finally:
        store.close()


def test_removed_system_step_kind_is_rejected(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_corrupted_kind",
            thread_id="term_corrupted_kind",
            origin="chat",
            input=Message.user("hello"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        _execute_sql(store.db_path, "UPDATE steps SET kind = 'system'")

        with pytest.raises(ValueError, match="invalid stored step kind"):
            store.list_steps(run_id=run.id)
    finally:
        store.close()


def test_invalid_execution_ids_are_rejected_before_any_rows_are_written(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runs.db"
    store = RunStore(db_path)
    try:
        store.create_thread(thread_id="term_valid")
        before_runs = _table_count(db_path, "runs")
        before_controls = _table_count(db_path, "controls")
        before_threads = _table_count(db_path, "threads")

        with pytest.raises(ValueError, match="invalid run id"):
            accept_run_start(
                store,
                run_id="run.bad",
                parent=None,
                thread="term_valid",
                input=Message.user("invalid"),
                context={},
                request_id=None,
                created_at="2026-01-01T00:00:00Z",
            )
        with pytest.raises(ValueError, match="invalid thread id"):
            store.create_thread(thread_id="thread.bad")

        assert _table_count(db_path, "runs") == before_runs
        assert _table_count(db_path, "controls") == before_controls
        assert _table_count(db_path, "threads") == before_threads
    finally:
        store.close()


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
            BEFORE INSERT ON controls
            WHEN NEW.kind = 'start'
            BEGIN
                SELECT RAISE(ABORT, 'injected start-control failure');
            END;
            """,
        )

        with pytest.raises(ValueError):
            accept_run_start(
                store,
                run_id="run_atomic_start",
                parent=None,
                thread="term_atomic_start",
                input=Message.user("hello"),
                context={},
                request_id="atomic-start",
                created_at="2026-01-01T00:00:00Z",
            )

        assert store.get_run(run_id="run_atomic_start") is None
        assert _table_count(store.db_path, "controls") == 1
    finally:
        store.close()


def test_retry_reopens_root_from_a_failed_value_step(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_retry",
            thread_id="term_retry",
            origin="chat",
            input=Message.user("hello"),
            executable_kind="flow",
        )
        first = project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        upstream = project_step(
            store,
            run_id=run.id,
            step_index=1,
            kind="tool",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        failed = project_step(
            store,
            run_id=run.id,
            step_index=2,
            kind="value",
            status="failed",
            input=(),
            output=(),
            error="temporary failure",
            started_at="2026-01-01T00:00:02Z",
            finished_at="2026-01-01T00:00:03Z",
        )
        project_run_end(
            store,
            run_id=run.id,
            status="failed",
            error="temporary failure",
        )

        start = store.get_run_control(run_id=run.id, index=0)
        assert start is not None
        assert isinstance(start.payload, StartControlPayload)
        reopened, control, ejected = store.accept_retry(
            run_id=run.id,
            anchor=None,
            resources=start.payload.resources,
            limits=start.payload.limits,
            runnable=start.payload.runnable,
            model=start.payload.model,
            locals=start.payload.locals,
            request_id="retry-1",
            created_at="2026-01-01T00:00:03Z",
        )

        assert reopened.status == "pending"
        assert reopened.started_at == ""
        assert reopened.finished_at is None
        assert reopened.error is None
        assert control.kind == "retry"
        assert isinstance(control.payload, RetryControlPayload)
        assert control.payload.retry_from == failed.path
        assert control.status == "applied"
        assert ejected == (failed.path,)
        assert store.list_steps(run_id=run.id) == [first, upstream]
        historical = store.list_steps(run_id=run.id, include_ejected=True)
        assert [step.path for step in historical] == [
            first.path,
            upstream.path,
            failed.path,
        ]
        assert historical[0].ejected_by is None
        assert historical[1].ejected_by is None
        assert historical[2].ejected_by is not None
        assert historical[2].ejected_by.run == run.id
        assert historical[2].ejected_by.index == control.index
    finally:
        store.close()


@pytest.mark.parametrize(
    (
        "run_status",
        "include_call",
        "explicit_anchor",
        "expected_anchor",
        "expected_ejected",
    ),
    [
        ("failed", True, None, 1, (1,)),
        ("succeeded", True, None, 0, (0, 1)),
        ("succeeded", True, 1, 1, (1,)),
        ("succeeded", False, None, 0, (0,)),
    ],
)
def test_retry_anchor_selection_distinguishes_run_outcomes_and_explicit_values(
    tmp_path: Path,
    run_status: RunStatus,
    include_call: bool,
    explicit_anchor: int | None,
    expected_anchor: int,
    expected_ejected: tuple[int, ...],
) -> None:
    store = RunStore(tmp_path / f"{run_status}.db")
    try:
        run = project_run_start(
            store,
            run_id=f"run_retry_{run_status}",
            thread_id=f"term_retry_{run_status}",
            origin="chat",
            input=Message.user("hello"),
            executable_kind="flow",
        )
        steps = []
        if include_call:
            steps.append(
                project_step(
                    store,
                    run_id=run.id,
                    step_index=0,
                    kind="model",
                    status="succeeded",
                    input=(),
                    output=(),
                    started_at="2026-01-01T00:00:00Z",
                    finished_at="2026-01-01T00:00:01Z",
                )
            )
        value = project_step(
            store,
            run_id=run.id,
            step_index=len(steps),
            kind="value",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        steps.append(value)
        project_run_end(
            store,
            run_id=run.id,
            status=run_status,
            error="runtime failure" if run_status == "failed" else None,
        )

        start = store.get_run_control(run_id=run.id, index=0)
        assert start is not None
        assert isinstance(start.payload, StartControlPayload)
        _reopened, control, ejected = store.accept_retry(
            run_id=run.id,
            anchor=steps[explicit_anchor].path if explicit_anchor is not None else None,
            resources=start.payload.resources,
            limits=start.payload.limits,
            runnable=start.payload.runnable,
            model=start.payload.model,
            locals=start.payload.locals,
            request_id=None,
            created_at="2026-01-01T00:00:03Z",
        )

        assert isinstance(control.payload, RetryControlPayload)
        assert control.payload.retry_from == steps[expected_anchor].path
        assert ejected == tuple(steps[index].path for index in expected_ejected)
    finally:
        store.close()


def test_rerun_acceptance_ejects_the_source_with_the_new_start_control(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        source = project_run_start(
            store,
            run_id="run_source",
            thread_id="term_rerun",
            origin="chat",
            input=Message.user("hello"),
            created_at="2026-01-01T00:00:00Z",
            started_at="2026-01-01T00:00:00Z",
        )
        project_run_end(
            store,
            run_id=source.id,
            finished_at="2026-01-01T00:00:01Z",
        )

        rerun, control = accept_run_start(
            store,
            run_id="run_rerun",
            parent=None,
            thread=source.thread,
            input=Message.user("hello"),
            context={"root": "run_rerun"},
            request_id="rerun-1",
            created_at="2026-01-01T00:00:02Z",
            kind="rerun",
            source=source.id,
        )

        stored_source = store.get_run(run_id=source.id)
        assert stored_source is not None
        assert stored_source.ejected_by == RunControlRef(rerun.id, 0)
        assert control.kind == "rerun"
        assert isinstance(control.payload, RerunControlPayload)
        assert control.payload.rerun_from == source.id
        assert [run.id for run in store.list_runs(limit=None)] == [rerun.id]
        assert [
            run.id for run in store.list_runs(limit=None, include_ejected=True)
        ] == [rerun.id, source.id]
    finally:
        store.close()


def test_step_and_control_projection_roll_back_as_one_write_unit(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        store.create_thread(thread_id="term_atomic_event")
        accept_run_start(
            store,
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
            locals=(Local.typed("Part[]", Message.user("updated").parts, "_", 0),),
            request_id=None,
            created_at="2026-01-01T00:00:01Z",
        )
        _execute_sql(
            store.db_path,
            f"""
            CREATE TRIGGER reject_control_finish
            BEFORE UPDATE OF status ON controls
            WHEN OLD.target = 'run_atomic_event' AND OLD."index" = {control.index}
            BEGIN
                SELECT RAISE(ABORT, 'injected control-finish failure');
            END;
            """,
        )

        with pytest.raises(sqlite3.IntegrityError):
            with store.write_transaction():
                store.begin_step(
                    path=StepPath("run_atomic_event", (0,)),
                    kind="value",
                    input=(Pointer.control("run_atomic_event", control.index, "_"),),
                    placement=None,
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
                    placement=None,
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
        accept_run_start(
            store,
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
            locals=(Local.typed("Part[]", Message.user("updated").parts, "_", 0),),
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
        assert changed[0].status == "revoked"
        assert canceled_revision > accepted_revision
    finally:
        store.close()


def test_run_store_rejects_a_legacy_execution_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)
    store.create_thread(thread_id="term_v18_revision")
    accept_run_start(
        store,
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
        connection.execute("PRAGMA user_version=25")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RunStoreSchemaError):
        RunStore(path)


def test_claimed_control_cannot_be_canceled_before_its_event_is_persisted(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        store.create_thread(thread_id="term_claimed_control")
        accept_run_start(
            store,
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
            locals=(Local.typed("Part[]", Message.user("updated").parts, "_", 0),),
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
