"""Retired retry history stays readable without enabling retry execution."""

import asyncio
from dataclasses import replace
import json

from typer.core import TyperGroup
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
import pytest
import typer

from tests.support.execution_harness import ExecutionHarness, TEST_MODEL_REF
from toolang.api.app import create_app
from toolang.base.types.message import TextPart
from toolang.base.types.policy import RunBindings
from toolang.catalog import CapsManager, ChoresManager
from toolang.cli.toolang.main import app as cli
from toolang.execution.client import LocalRunClient, RunClient
from toolang.execution.events import event_from_data
from toolang.execution.inspection.history import RunHistory
from toolang.execution.records import (
    RunControlPayload,
    control_payload_from_data,
    control_payload_to_data,
    run_preparation,
)
from toolang.execution.remote import RemoteRunClient
from toolang.execution.schemas import RerunRequest, RunDetail
from toolang.execution.types import ControlRef, ThreadPrefix
from toolang.up.core import AgentCore


@pytest.mark.parametrize("invalidation_lists", [False, True])
def test_legacy_retry_history_is_inspectable_and_an_ordinary_rerun_source(
    tmp_path, invalidation_lists
):
    async def scenario():
        harness = ExecutionHarness.create(
            tmp_path, source="flow example:\n  let result = Done\n", responses=[]
        )
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.CHAT)
            source = await harness.executor.run(
                replace(
                    harness.run_spec(
                        thread=thread, runnable="example", primary=(TextPart("input"),)
                    ),
                    bindings=RunBindings(runnable="flow:example"),
                    model_request=None,
                )
            )
            initial = harness.store.list_run_controls(run_id=source.id)[0]
            assert isinstance(initial.payload, RunControlPayload)
            legacy: dict[str, object] = {
                "resources": initial.payload.resources.to_data(),
                "limits": {"tokens": 321},
                "model_request": {"ref": TEST_MODEL_REF},
                "retry_from": f"{source.id}.0",
            }
            if invalidation_lists:
                legacy.update(
                    invalidated_steps=[f"{source.id}.1"], removed_runs=["run_old"]
                )
            # Model an existing database from the retired writer. Production
            # mutation methods must never create this historical control again.
            control = ControlRef.for_run(source.id, 1)
            with harness.store._conn:
                harness.store._conn.execute(
                    "INSERT INTO controls(id,scope,target,\"index\",kind,request,status,error,timing,payload,created_at,finished_at,_claimed,_revision,triggered_by,event_cursor,client_id) SELECT ?,scope,target,1,'retry',NULL,'applied',NULL,"
                    "timing,?,created_at,finished_at,_claimed,_revision+1,NULL,NULL,NULL "
                    "FROM controls WHERE id=?",
                    (str(control), json.dumps(legacy), initial.id),
                )
                harness.store._conn.execute(
                    "UPDATE runs SET control=? WHERE id=?", (str(control), source.id)
                )
            original = harness.store.get_run(run_id=source.id)
            assert original is not None
            controls = harness.store.list_run_controls(run_id=source.id)
            steps = harness.store.list_steps(run_id=source.id)
            prepared = run_preparation(original, controls)
            assert prepared.limits.tokens == 321
            assert prepared.model_request is not None
            assert prepared.model_request.ref == TEST_MODEL_REF
            detail = RunHistory(harness.store).get_run(source.id)
            assert detail is not None
            encoded = TypeAdapter(RunDetail).dump_python(detail, mode="json")
            assert encoded["controls"][-1]["payload"] == legacy
            assert (
                control_payload_to_data(control_payload_from_data("retry", legacy))
                == legacy
            )

            with pytest.raises(ValueError, match="legacy retry controls are read-only"):
                harness.store._insert_control(
                    ref=ControlRef.for_run(source.id, 2),
                    kind="retry",
                    timing="immediate",
                    payload=control_payload_from_data("retry", legacy),
                    request=None,
                    status="applied",
                    error=None,
                    created_at=source.created_at,
                    finished_at=source.finished_at,
                    claimed=True,
                )
            assert harness.store.list_run_controls(run_id=source.id) == controls

            rerun = await harness.executor.rerun(
                RerunRequest(source=source.id, commands=(), request_id="ordinary_rerun")
            )
            assert rerun.id != source.id and rerun.status == "succeeded"
            new_control = harness.store.list_run_controls(run_id=rerun.id)[0]
            assert new_control.kind == "run"
            assert isinstance(new_control.payload, RunControlPayload)
            assert new_control.payload.model_request == prepared.model_request
            assert harness.store.get_run(run_id=source.id) == original
            assert harness.store.list_run_controls(run_id=source.id) == controls
            assert harness.store.list_steps(run_id=source.id) == steps

    asyncio.run(scenario())


def test_retry_and_activity_routes_and_commands_are_unavailable(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )
    harness.store.close()
    core = AgentCore(harness.setup.layout)
    try:
        app = create_app(core, CapsManager(core.layout), ChoresManager(core.layout))
        with TestClient(app) as http:
            assert http.get("/api/v1/activity").status_code == 404
            for prefix in ("runs", "chat/runs"):
                for suffix in ("retry", "retry/stream"):
                    assert (
                        http.post(
                            f"/api/v1/{prefix}/run_old/{suffix}", json={}
                        ).status_code
                        == 404
                    )
        commands = typer.main.get_command(cli)
        assert isinstance(commands, TyperGroup)
        assert "retry" not in commands.commands
        assert "top" not in commands.commands
        for owner in (
            core.executor,
            core.store,
            RunClient,
            LocalRunClient,
            RemoteRunClient,
        ):
            assert not hasattr(owner, "retry")
        assert not hasattr(core.store, "accept_retry")
        assert not hasattr(core, "activity")
        with pytest.raises(ValueError):
            event_from_data({"type": "run_retried", "run": "run_old"})
    finally:
        asyncio.run(core.close())
