"""Retired retry formats and routes are rejected."""

import asyncio

from typer.core import TyperGroup
from fastapi.testclient import TestClient
import pytest
import typer

from tests.support.execution_harness import ExecutionHarness
from toolang.api.app import create_app
from toolang.catalog import CapsManager, ChoresManager
from toolang.cli.toolang.main import app as cli
from toolang.execution.client import LocalRunClient, RunClient
from toolang.execution.events import event_from_data
from toolang.execution.records import (
    control_payload_from_data,
)
from toolang.execution.remote import RemoteRunClient
from toolang.up.core import AgentCore


def test_legacy_retry_payload_is_rejected():
    from typing import cast
    from toolang.execution.types import ControlKind

    with pytest.raises(ValueError, match="unknown control kind: retry"):
        control_payload_from_data(cast(ControlKind, "retry"), {})


def test_retry_and_legacy_activity_routes_remain_unavailable(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )
    harness.store.close()
    core = AgentCore(harness.setup.layout)
    try:
        app = create_app(core, CapsManager(core.layout), ChoresManager(core.layout))
        with TestClient(app) as http:
            assert http.get("/api/v0/activity").status_code == 404
            for prefix in ("runs", "chat/runs"):
                for suffix in ("retry", "retry/stream"):
                    assert (
                        http.post(
                            f"/api/v0/{prefix}/run_old/{suffix}", json={}
                        ).status_code
                        == 404
                    )
        commands = typer.main.get_command(cli)
        assert isinstance(commands, TyperGroup)
        assert "retry" not in commands.commands
        assert "top" in commands.commands
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
