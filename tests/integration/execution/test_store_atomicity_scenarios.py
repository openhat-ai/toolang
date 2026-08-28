"""Run-store transaction and durable integrity scenarios."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from tests.support.execution_fixtures import (
    accept_run,
    project_run_end,
    project_run_start,
    project_step,
)
from toolang.base.types.message import Message, ToolCallPart
from toolang.base.types.run import ModelCall
from toolang.base.types.tool import ToolDefinition
from toolang.execution.errors import RunStoreSchemaError
from toolang.execution.records import (
    RerunControlPayload,
    ReloadControlPayload,
    RetryControlPayload,
    RunControlPayload,
)
from toolang.execution.store import RunStore
from toolang.execution.types import (
    ControlRef,
    Local,
    ModelStepGiven,
    Pointer,
    RunStatus,
    StepPath,
)
from toolang.lang.ast import LetStmt, Span


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


def test_run_store_persists_dot_separated_step_paths(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        root = project_run_start(
            store,
            run_id="run_dot_path",
            thread_id="term_dot_path",
            origin="chat",
            input=Message.user("hello"),
        )
        step = project_step(
            store,
            parent=StepPath(root.id, (2,)),
            index=3,
            kind="value",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        project_run_start(
            store,
            run_id="run_dot_child",
            thread_id=root.thread,
            origin="chat",
            input=Message.user("child"),
            parent=step.path,
        )

        connection = sqlite3.connect(store.db_path)
        try:
            assert connection.execute(
                "SELECT path FROM steps WHERE run = ?", (root.id,)
            ).fetchone() == ("2.3",)
            assert connection.execute(
                "SELECT parent FROM runs WHERE id = 'run_dot_child'"
            ).fetchone() == ("run_dot_path.2.3",)
            assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 31
        finally:
            connection.close()
    finally:
        store.close()


@pytest.mark.parametrize(
    ("table", "column", "invalid", "message"),
    (
        ("runs", "output", "[]", "stored run output must be an object"),
        ("runs", "occurrence", "[]", "stored run occurrence must be an object"),
        ("steps", "input", "{}", "stored step input must be an array"),
        ("steps", "output", "[]", "stored step output must be an object"),
        ("steps", "occurrence", "[]", "stored step occurrence must be an object"),
        ("steps", "given", "[]", "stored step given must be an object"),
        (
            "steps",
            "noted",
            "[]",
            "collection noted requires exactly: output_items, total_items",
        ),
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
            accept_run(
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


def test_run_acceptance_rolls_back_the_run_when_control_insert_fails(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        store.create_thread(thread_id="term_atomic_start")
        _execute_sql(
            store.db_path,
            """
            CREATE TRIGGER reject_run_control
            BEFORE INSERT ON controls
            WHEN NEW.kind = 'run'
            BEGIN
                SELECT RAISE(ABORT, 'injected run-control failure');
            END;
            """,
        )

        with pytest.raises(ValueError):
            accept_run(
                store,
                run_id="run_atomic_start",
                parent=None,
                thread="term_atomic_start",
                input=Message.user("hello"),
                context={},
                request_id="atomic-run",
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
            runnable_kind="flow",
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

        run_control = store.get_run_control(run_id=run.id, index=0)
        assert run_control is not None
        assert isinstance(run_control.payload, RunControlPayload)
        with pytest.raises(
            ValueError,
            match="retry state no longer matches original run.*use rerun",
        ):
            store.accept_retry(
                run_id=run.id,
                anchor=None,
                resources=run_control.payload.resources,
                limits=run_control.payload.limits,
                runnable=run_control.payload.runnable,
                model=run_control.payload.model,
                locals=run_control.payload.locals,
                sandbox="host",
                state="1" * 64,
                request_id="retry-mismatch",
                created_at="2026-01-01T00:00:03Z",
            )
        unchanged = store.get_run(run_id=run.id)
        assert unchanged is not None and unchanged.status == "failed"

        _execute_sql(
            store.db_path,
            """
            CREATE TRIGGER reject_retry_control
            BEFORE INSERT ON controls
            WHEN NEW.kind = 'retry'
            BEGIN
                SELECT RAISE(ABORT, 'injected retry-control failure');
            END;
            """,
        )
        with pytest.raises(ValueError):
            store.accept_retry(
                run_id=run.id,
                anchor=None,
                resources=run_control.payload.resources,
                limits=run_control.payload.limits,
                runnable=run_control.payload.runnable,
                model=run_control.payload.model,
                locals=run_control.payload.locals,
                sandbox="host",
                state=run_control.payload.state,
                request_id="retry-rollback",
                created_at="2026-01-01T00:00:03Z",
            )
        assert store.list_steps(run_id=run.id) == [first, upstream, failed]
        unchanged = store.get_run(run_id=run.id)
        assert unchanged is not None and unchanged.status == "failed"
        assert len(store.list_run_controls(run_id=run.id)) == 1
        _execute_sql(store.db_path, "DROP TRIGGER reject_retry_control;")

        reopened, control, trimmed = store.accept_retry(
            run_id=run.id,
            anchor=None,
            resources=run_control.payload.resources,
            limits=run_control.payload.limits,
            runnable=run_control.payload.runnable,
            model=run_control.payload.model,
            locals=run_control.payload.locals,
            sandbox="host",
            state=run_control.payload.state,
            request_id="retry-rollback",
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
        assert trimmed == (failed.path,)
        assert store.list_steps(run_id=run.id) == [first, upstream]
        assert store.list_steps(run_id=run.id, include_ejected=True) == [
            first,
            upstream,
        ]
    finally:
        store.close()


def test_reload_control_records_state_and_has_one_claim_or_revocation_winner(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_reload_lifecycle",
            thread_id="term_reload_lifecycle",
            origin="chat",
            input=Message.user("hello"),
            runnable_kind="flow",
        )
        assert run.state == ControlRef(run.id, 0)
        step = project_step(
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
        assert step.state == run.state

        with pytest.raises(ValueError, match="immediate timing"):
            store.accept_reload_control(
                run_id=run.id,
                state="1" * 64,
                timing="next_step",
                request_id="reload-invalid-timing",
                created_at="2026-01-01T00:00:02Z",
            )
        claimed = store.accept_reload_control(
            run_id=run.id,
            state="1" * 64,
            request_id="reload-claimed",
            created_at="2026-01-01T00:00:02Z",
        )
        assert isinstance(claimed.payload, ReloadControlPayload)
        assert store.resolve_state_revision(ControlRef(run.id, claimed.index)) == (
            "1" * 64
        )
        assert store.claim_run_controls(run_id=run.id, indexes=(claimed.index,)) == {
            claimed.index
        }
        with pytest.raises(ValueError, match="already being applied"):
            store.cancel_run_control(
                run_id=run.id,
                index=claimed.index,
                canceled_at="2026-01-01T00:00:03Z",
            )
        store.finish_run_controls(
            run_id=run.id,
            indexes=(claimed.index,),
            finished_at="2026-01-01T00:00:03Z",
        )

        revoked = store.accept_reload_control(
            run_id=run.id,
            state="2" * 64,
            request_id="reload-revoked",
            created_at="2026-01-01T00:00:04Z",
        )
        store.cancel_run_control(
            run_id=run.id,
            index=revoked.index,
            canceled_at="2026-01-01T00:00:05Z",
        )
        assert (
            store.claim_run_controls(run_id=run.id, indexes=(revoked.index,)) == set()
        )
    finally:
        store.close()


@pytest.mark.parametrize("unapplied_status", ("revoked", "wontapply"))
def test_retry_allows_unapplied_reload_history(
    tmp_path: Path,
    unapplied_status: str,
) -> None:
    store = RunStore(tmp_path / f"{unapplied_status}.db")
    try:
        run = project_run_start(
            store,
            run_id=f"run_reload_{unapplied_status}",
            thread_id=f"term_reload_{unapplied_status}",
            origin="chat",
            input=Message.user("hello"),
            runnable_kind="flow",
        )
        reload_control = store.accept_reload_control(
            run_id=run.id,
            state="1" * 64,
            request_id=f"reload-{unapplied_status}",
            created_at="2026-01-01T00:00:01Z",
        )
        if unapplied_status == "revoked":
            store.cancel_run_control(
                run_id=run.id,
                index=reload_control.index,
                canceled_at="2026-01-01T00:00:02Z",
            )
        else:
            store.fail_pending_run_controls(
                run_id=run.id,
                finished_at="2026-01-01T00:00:02Z",
                error="run ended before the control could be applied",
            )
        project_run_end(store, run_id=run.id)
        entry = store.get_run_control(run_id=run.id, index=0)
        assert entry is not None and isinstance(entry.payload, RunControlPayload)
        reopened, retry, _trimmed = store.accept_retry(
            run_id=run.id,
            anchor=None,
            resources=entry.payload.resources,
            limits=entry.payload.limits,
            state=entry.payload.state,
            runnable=entry.payload.runnable,
            model=entry.payload.model,
            locals=entry.payload.locals,
            sandbox="host",
            request_id=f"retry-{unapplied_status}",
            created_at="2026-01-01T00:00:03Z",
        )
        assert reopened.state == ControlRef(run.id, 0)
        assert isinstance(retry.payload, RetryControlPayload)
        assert retry.payload.state is None
    finally:
        store.close()


def test_retry_rejects_applied_reload_history_without_mutation(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_reload_applied",
            thread_id="term_reload_applied",
            origin="chat",
            input=Message.user("hello"),
            runnable_kind="flow",
        )
        reload_control = store.accept_reload_control(
            run_id=run.id,
            state="1" * 64,
            request_id="reload-applied",
            created_at="2026-01-01T00:00:01Z",
        )
        assert store.claim_run_controls(
            run_id=run.id, indexes=(reload_control.index,)
        ) == {reload_control.index}
        store.finish_run_controls(
            run_id=run.id,
            indexes=(reload_control.index,),
            finished_at="2026-01-01T00:00:02Z",
        )
        project_run_end(store, run_id=run.id)
        entry = store.get_run_control(run_id=run.id, index=0)
        assert entry is not None and isinstance(entry.payload, RunControlPayload)

        with pytest.raises(ValueError, match="applied Agent State reloads.*use rerun"):
            store.accept_retry(
                run_id=run.id,
                anchor=None,
                resources=entry.payload.resources,
                limits=entry.payload.limits,
                state=entry.payload.state,
                runnable=entry.payload.runnable,
                model=entry.payload.model,
                locals=entry.payload.locals,
                sandbox="host",
                request_id="retry-applied-reload",
                created_at="2026-01-01T00:00:03Z",
            )
        unchanged = store.get_run(run_id=run.id)
        assert unchanged is not None and unchanged.status == "succeeded"
    finally:
        store.close()


def test_retry_rejects_applied_execute_history_without_mutation(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_execute_applied",
            thread_id="term_execute_applied",
            origin="chat",
            input=Message.user("hello"),
            runnable_kind="agic",
        )
        model = project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="model",
            status="succeeded",
            input=(),
            output=(
                ToolCallPart(
                    tool_call_id="execute",
                    tool_name="_too__execute",
                    tool_family="_too__execute",
                    input={"runnable": "target", "input": {"_": "work"}},
                ),
            ),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        entry = store.get_run_control(run_id=run.id, index=0)
        assert entry is not None and isinstance(entry.payload, RunControlPayload)
        assert entry.payload.state is not None
        source = Pointer.step(model.path, 0)
        execute = store.accept_execute_control(
            run_id=run.id,
            state=entry.payload.state,
            runnable="agic:target",
            module="agent",
            source=source,
            locals=(Local.typed("Json", source.select("input", "input", "_"), "_"),),
            created_at="2026-01-01T00:00:03Z",
        )
        assert execute.status == "applied"
        project_run_end(store, run_id=run.id)

        with pytest.raises(ValueError, match="applied execute controls.*use rerun"):
            store.accept_retry(
                run_id=run.id,
                anchor=None,
                resources=entry.payload.resources,
                limits=entry.payload.limits,
                state=entry.payload.state,
                runnable=entry.payload.runnable,
                model=entry.payload.model,
                locals=entry.payload.locals,
                sandbox="host",
                request_id="retry-applied-execute",
                created_at="2026-01-01T00:00:04Z",
            )
        unchanged = store.get_run(run_id=run.id)
        assert unchanged is not None and unchanged.status == "succeeded"
    finally:
        store.close()


def test_retry_rejects_unknown_or_mismatched_sandbox_without_mutation(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_sandbox_retry",
            thread_id="term_sandbox_retry",
            origin="chat",
            input=Message.user("hello"),
        )
        project_run_end(store, run_id=run.id, status="succeeded")
        run_control = store.get_run_control(run_id=run.id, index=0)
        assert run_control is not None
        assert isinstance(run_control.payload, RunControlPayload)
        run_payload = run_control.payload
        assert run_payload.sandbox == "host"

        def accept_retry(sandbox: str):
            return store.accept_retry(
                run_id=run.id,
                anchor=None,
                resources=run_payload.resources,
                limits=run_payload.limits,
                state=run_payload.state,
                runnable=run_payload.runnable,
                model=run_payload.model,
                locals=run_payload.locals,
                sandbox=sandbox,
                request_id=None,
                created_at="2026-01-01T00:00:03Z",
            )

        with pytest.raises(
            ValueError,
            match="does not match original sandbox.*use rerun",
        ):
            accept_retry("docker:python:3.13-slim")
        assert len(store.list_run_controls(run_id=run.id)) == 1

        _execute_sql(
            store.db_path,
            """
            UPDATE controls
            SET payload = json_remove(payload, '$.sandbox')
            WHERE target = 'run_sandbox_retry' AND "index" = 0;
            """,
        )
        legacy = store.get_run_control(run_id=run.id, index=0)
        assert legacy is not None
        assert isinstance(legacy.payload, RunControlPayload)
        assert legacy.payload.sandbox is None

        with pytest.raises(ValueError, match="sandbox is unknown.*use rerun"):
            accept_retry("host")
        unchanged = store.get_run(run_id=run.id)
        assert unchanged is not None and unchanged.status == "succeeded"
        assert len(store.list_run_controls(run_id=run.id)) == 1
    finally:
        store.close()


def test_retry_preserves_child_controls_and_revision_monotonicity(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        root = project_run_start(
            store,
            run_id="run_retry_controls",
            thread_id="term_retry_controls",
            origin="chat",
            input=Message.user("root"),
            runnable_kind="flow",
        )
        parent = project_step(
            store,
            run_id=root.id,
            step_index=0,
            kind="run",
            status="failed",
            input=(),
            output=(),
            error="temporary failure",
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        child = project_run_start(
            store,
            run_id="run_retry_child",
            thread_id=root.thread,
            origin="chat",
            input=Message.user("child"),
            parent=parent.path,
        )
        pending = store.accept_run_control(
            run_id=child.id,
            kind="steer",
            timing="next_step",
            locals=(Local.typed("Part[]", Message.user("guidance").parts, "_", 0),),
            request_id="child-steer-request",
            created_at="2026-01-01T00:00:02Z",
        )
        project_run_end(
            store,
            run_id=child.id,
            status="failed",
            error="temporary failure",
        )
        project_run_end(
            store,
            run_id=root.id,
            status="failed",
            error="temporary failure",
        )
        control = store.get_run_control(run_id=root.id, index=0)
        assert control is not None and isinstance(control.payload, RunControlPayload)
        payload = control.payload
        revision = store.latest_run_control_revision()

        store.accept_retry(
            run_id=root.id,
            anchor=None,
            resources=payload.resources,
            limits=payload.limits,
            state=payload.state,
            runnable=payload.runnable,
            model=payload.model,
            locals=payload.locals,
            sandbox="host",
            request_id="retry-control-request",
            created_at="2026-01-01T00:00:03Z",
        )

        latest, changed = store.changed_run_controls(after_revision=revision)
        assert latest > revision
        assert [(item.run, item.kind, item.status) for item in changed] == [
            (root.id, "retry", "applied"),
            (child.id, "steer", "wontapply"),
        ]
        assert store.get_run(run_id=child.id) is None
        assert store.get_run_control(run_id=child.id, index=0) is not None
        stored_pending = store.get_run_control(run_id=child.id, index=pending.index)
        assert stored_pending is not None
        assert stored_pending.status == "wontapply"
        assert stored_pending.error == "run retried before the control could be applied"
        with pytest.raises(
            ValueError,
            match="run control request already exists: child-steer-request",
        ):
            store.accept_run_control(
                run_id=root.id,
                kind="steer",
                timing="next_step",
                locals=(
                    Local.typed(
                        "Part[]",
                        Message.user("duplicate").parts,
                        "_",
                        0,
                    ),
                ),
                request_id="child-steer-request",
                created_at="2026-01-01T00:00:04Z",
            )
    finally:
        store.close()


@pytest.mark.parametrize(
    (
        "run_status",
        "include_call",
        "explicit_anchor",
        "expected_anchor",
        "expected_trimmed",
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
    expected_trimmed: tuple[int, ...],
) -> None:
    store = RunStore(tmp_path / f"{run_status}.db")
    try:
        run = project_run_start(
            store,
            run_id=f"run_retry_{run_status}",
            thread_id=f"term_retry_{run_status}",
            origin="chat",
            input=Message.user("hello"),
            runnable_kind="flow",
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

        run_control = store.get_run_control(run_id=run.id, index=0)
        assert run_control is not None
        assert isinstance(run_control.payload, RunControlPayload)
        _reopened, control, trimmed = store.accept_retry(
            run_id=run.id,
            anchor=steps[explicit_anchor].path if explicit_anchor is not None else None,
            resources=run_control.payload.resources,
            limits=run_control.payload.limits,
            runnable=run_control.payload.runnable,
            model=run_control.payload.model,
            locals=run_control.payload.locals,
            sandbox="host",
            state=run_control.payload.state,
            request_id=None,
            created_at="2026-01-01T00:00:03Z",
        )

        assert isinstance(control.payload, RetryControlPayload)
        assert control.payload.retry_from == steps[expected_anchor].path
        expected_paths = tuple(steps[index].path for index in expected_trimmed)
        assert trimmed == expected_paths
        assert {
            step.path for step in store.list_steps(run_id=run.id, include_ejected=True)
        } == {step.path for step in steps} - set(expected_paths)
    finally:
        store.close()


def test_rerun_acceptance_preserves_the_source_and_allows_repeated_reruns(
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

        rerun, control = accept_run(
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

        second_rerun, second_control = accept_run(
            store,
            run_id="run_rerun_again",
            parent=None,
            thread=source.thread,
            input=Message.user("hello"),
            context={"root": "run_rerun_again"},
            request_id="rerun-2",
            created_at="2026-01-01T00:00:03Z",
            kind="rerun",
            source=source.id,
        )

        stored_source = store.get_run(run_id=source.id)
        assert stored_source is not None
        assert stored_source.ejected_by is None
        assert control.kind == "rerun"
        assert isinstance(control.payload, RerunControlPayload)
        assert control.payload.rerun_from == source.id
        assert second_control.kind == "rerun"
        assert isinstance(second_control.payload, RerunControlPayload)
        assert second_control.payload.rerun_from == source.id
        assert [run.id for run in store.list_runs(limit=None)] == [
            second_rerun.id,
            rerun.id,
            source.id,
        ]
    finally:
        store.close()


def test_rerun_does_not_read_or_write_source_ejection(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        anchor = project_run_start(
            store,
            run_id="run_rerun_anchor",
            thread_id="term_rerun_ejected",
            origin="chat",
            input=Message.user("anchor"),
        )
        project_run_end(store, run_id=anchor.id)
        source = project_run_start(
            store,
            run_id="run_rerun_ejected",
            thread_id=anchor.thread,
            origin="chat",
            input=Message.user("source"),
        )
        project_run_end(store, run_id=source.id)
        thread = store.get_thread(thread_id=source.thread)
        assert thread is not None
        store.rewind_thread(
            thread_id=source.thread,
            anchor=anchor.id,
            request_id=None,
            expected_head=thread.head,
            created_at="2026-01-01T00:00:02Z",
        )
        ejected_source = store.get_run(run_id=source.id)
        assert ejected_source is not None and ejected_source.ejected_by is not None

        rerun, control = accept_run(
            store,
            run_id="run_from_ejected_source",
            parent=None,
            thread=source.thread,
            input=Message.user("source"),
            context={},
            request_id="rerun-ejected-source",
            created_at="2026-01-01T00:00:03Z",
            kind="rerun",
            source=source.id,
        )

        unchanged_source = store.get_run(run_id=source.id)
        assert unchanged_source is not None
        assert unchanged_source.ejected_by == ejected_source.ejected_by
        assert rerun.ejected_by is None
        assert isinstance(control.payload, RerunControlPayload)
        assert control.payload.rerun_from == source.id
    finally:
        store.close()


def test_retry_does_not_read_or_write_ejection_fields(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        anchor = project_run_start(
            store,
            run_id="run_retry_anchor",
            thread_id="term_retry_ejected",
            origin="chat",
            input=Message.user("anchor"),
        )
        project_run_end(store, run_id=anchor.id)
        source = project_run_start(
            store,
            run_id="run_retry_ejected",
            thread_id=anchor.thread,
            origin="chat",
            input=Message.user("source"),
            runnable_kind="flow",
        )
        retained = project_step(
            store,
            run_id=source.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        failed = project_step(
            store,
            run_id=source.id,
            step_index=1,
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
            run_id=source.id,
            status="failed",
            error="temporary failure",
        )
        thread = store.get_thread(thread_id=source.thread)
        assert thread is not None
        _thread, rewind, _ejected = store.rewind_thread(
            thread_id=source.thread,
            anchor=anchor.id,
            request_id=None,
            expected_head=thread.head,
            created_at="2026-01-01T00:00:04Z",
        )
        _execute_sql(
            store.db_path,
            f"""
            UPDATE steps
            SET ejected_by_target = '{rewind.thread}',
                ejected_by_index = {rewind.index}
            WHERE run = '{source.id}' AND path = '{failed.path.local}';
            """,
        )
        ejected_source = store.get_run(run_id=source.id)
        assert ejected_source is not None and ejected_source.ejected_by is not None
        control = store.get_run_control(run_id=source.id, index=0)
        assert control is not None and isinstance(control.payload, RunControlPayload)
        payload = control.payload

        reopened, retry, trimmed = store.accept_retry(
            run_id=source.id,
            anchor=None,
            resources=payload.resources,
            limits=payload.limits,
            state=payload.state,
            runnable=payload.runnable,
            model=payload.model,
            locals=payload.locals,
            sandbox="host",
            request_id="retry-ejected-source",
            created_at="2026-01-01T00:00:05Z",
        )

        assert reopened.ejected_by == ejected_source.ejected_by
        assert isinstance(retry.payload, RetryControlPayload)
        assert retry.payload.retry_from == failed.path
        assert trimmed == (failed.path,)
        assert store.list_steps(run_id=source.id, include_ejected=True) == [retained]
    finally:
        store.close()


def test_step_and_control_projection_roll_back_as_one_write_unit(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        store.create_thread(thread_id="term_atomic_event")
        accept_run(
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
                    occurrence=None,
                    given=LetStmt(span=Span(line=1), value="test"),
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
        with pytest.raises(sqlite3.IntegrityError):
            store.begin_step(
                path=StepPath("run_atomic_model", (0,)),
                kind="model",
                input=(),
                occurrence=None,
                given=ModelStepGiven(model="test/model", call=call),
                state=ControlRef("run_atomic_model", 0),
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
        accept_run(
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
    accept_run(
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
        accept_run(
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
