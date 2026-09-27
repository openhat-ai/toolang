"""Cwd controls share one durable commit boundary with successful chdir Steps."""

import pytest

from tests.support.execution_fixtures import project_run_start
from toolang.base.errors import ToolangError
from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.run import ToolCall
from toolang.execution.records import (
    CwdControlPayload,
    RunControlPayload,
    control_payload_from_data,
)
from toolang.execution.schemas import record_to_data
from toolang.execution.store import RunStore
from toolang.execution.types import Local, Output, StepRef, ToolStepGiven, ToolStepNoted


def test_cd_step_end_and_cwd_control_commit_atomically(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_cwd_atomic",
            thread_id="term_cwd_atomic",
            origin="chat",
            input=Message.user("start"),
        )
        ref = StepRef.from_local(run.id, (0,))
        call = ToolCall("chdir", "chdir", "_toolang__chdir", {"path": "repo://src"})
        store.begin_step(
            ref=ref,
            kind="tool",
            input=(),
            given=ToolStepGiven("_toolang", call),
            started_at="2026-09-27T00:00:00Z",
        )
        part = ToolResultPart(
            "chdir", "_toolang__chdir", "_toolang__chdir", {"cwd": "repo://src"}
        )

        def finish():
            return store.finish_step(
                ref=ref,
                kind="tool",
                status="succeeded",
                output=Output(Local.typed("ToolResultPart", part)),
                noted=ToolStepNoted(summary="Changed directory"),
                error=None,
                finished_at="2026-09-27T00:00:01Z",
            )

        original = store._insert_control

        def fail_control(*args, **kwargs):
            if kwargs.get("kind") == "cwd":
                raise RuntimeError("simulated interrupted transaction")
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "_insert_control", fail_control)
        with pytest.raises(RuntimeError, match="interrupted transaction"):
            finish()
        step = store.get_step(ref=ref)
        assert step is not None and step.status == "running"
        assert store.current_cwd(run.id) == ""
        assert store.list_run_controls(run_id=run.id, kind="cwd") == ()

        monkeypatch.setattr(store, "_insert_control", original)
        assert finish().status == "succeeded"
        (control,) = store.list_run_controls(run_id=run.id, kind="cwd")
        assert control.triggered_by == ref
        assert isinstance(control.payload, CwdControlPayload)
        assert control.payload.cwd == "repo://src"
        assert store.current_cwd(run.id) == "repo://src"
        reopened = RunStore(store.db_path)
        try:
            assert reopened.current_cwd(run.id) == "repo://src"
        finally:
            reopened.close()
    finally:
        store.close()


def test_legacy_run_control_without_cwd_starts_unselected(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_cwd_legacy",
            thread_id="term_cwd_legacy",
            origin="chat",
            input=Message.user("start"),
        )
        record = store.get_run_control(run_id=run.id, index=0)
        assert record is not None
        serialized = record_to_data(record)["payload"]
        assert isinstance(serialized, dict)
        payload = dict(serialized)
        del payload["cwd"]
        loaded = control_payload_from_data("run", payload)
        assert isinstance(loaded, RunControlPayload)
        assert loaded.cwd == ""
    finally:
        store.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"cwd": None, "cause": "cd"},
        {"cwd": 0, "cause": "cd"},
        {"cwd": "", "cause": "wrong"},
        {"cwd": "workspace://repo/", "cause": "cd"},
    ],
)
def test_cwd_control_rejects_noncanonical_or_uncaused_data(payload):
    with pytest.raises((ToolangError, TypeError, ValueError)):
        control_payload_from_data("cwd", payload)


def test_legacy_workspace_invalidation_without_fallback_remains_readable() -> None:
    payload = control_payload_from_data(
        "cwd",
        {"cwd": "", "cause": "invalidated", "state": "run_abc@0"},
    )

    assert isinstance(payload, CwdControlPayload)
    assert payload.cwd == ""
