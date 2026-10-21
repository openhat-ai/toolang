from toolang.up.workload import ForegroundWorkload
import asyncio

import httpx
import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.cli.toolang.main import main
from toolang.up.core import AgentCore
from toolang.common.layout import AgentLayout
from toolang.execution.types import ThreadKind
from toolang.state.types import StateSyncResult
from toolang.up.foreground import foreground_api
from toolang.up.identity import allocate_roaming
from toolang.up.instance import read_instance
from toolang.up.lifecycle import LAUNCH_HEADER, ServerLifecycle
from toolang.up.observation import RuntimeRegistration, runtime_inventory


@pytest.mark.parametrize("placement", ["resident", "visiting", "roaming"])
def test_shared_foreground_api_observes_and_cancels_the_owning_executor(
    tmp_path, monkeypatch, placement
):
    gate = AsyncGate()
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    if placement == "resident":
        layout = AgentLayout.resident(tmp_path, "alice")
        target = ["--root", str(tmp_path), "alice"]
    elif placement == "visiting":
        from toolang.up.source import HttpRef, materialize_visiting_program

        source_ref = "https://example.test/alice.too"
        layout = materialize_visiting_program(
            HttpRef(source_ref), "agic work():\n  Do work.\n", toolang_root=tmp_path
        )
        target = ["--root", str(tmp_path), source_ref]
    else:
        source = tmp_path / "alice.too"
        layout = allocate_roaming(source, toolang_root=tmp_path)
        target = [str(source)]
    instance = ["-i", layout.instance_id] if layout.instance_id else []
    harness = ExecutionHarness.create(
        layout.root,
        source="agic work():\n  Do work.\n",
        prepare_state=True,
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("done")), gate=gate
            )
        ],
    )

    async def scenario():
        with ForegroundWorkload(
            layout, toolang_root=tmp_path, drivers=frozenset()
        ) as registration:
            core = AgentCore(
                layout,
                store=harness.store,
                ids=harness.ids,
                initial_state=harness.state,
                drivers=frozenset(),
                lifecycle=ServerLifecycle(registration.token, "foreground"),
            )
            monkeypatch.setattr(core.setup, "current", lambda: harness.setup)
            monkeypatch.setattr(core.state, "current", lambda: harness.state)

            async def sync():
                return StateSyncResult(harness.state.revision, harness.state.files)

            monkeypatch.setattr(core.state, "sync", sync)
            try:
                async with foreground_api(core, registration):
                    thread = core.threads.create(kind=ThreadKind.EXEC)
                    handle = core.executor.run(
                        harness.run_spec(thread=thread, runnable="agic:work")
                    )
                    async with asyncio.timeout(5):
                        await gate.wait_until_entered()
                    entry = read_instance(layout.runtime_status)
                    assert entry is not None
                    endpoint = entry["workload"]["endpoint"]
                    async with httpx.AsyncClient(base_url=endpoint) as http:
                        response = await http.get("/api/v0/top/snapshot")
                        assert response.status_code == 200
                        statistics = await http.get("/api/v0/stats/summary")
                        assert statistics.status_code == 200, statistics.text
                        snapshot = response.json()
                        assert (
                            snapshot["live"]["trees"][0]["root_run_id"] == handle.run_id
                        )
                        inventory = await asyncio.to_thread(runtime_inventory, tmp_path)
                        assert [
                            item.identity for item in inventory.snapshot.instances
                        ] == [layout.instance_name]
                        rejected = await http.post(
                            f"/api/v0/runs/{handle.run_id}/cancel",
                            headers={LAUNCH_HEADER: "old-generation"},
                            json={},
                        )
                        assert rejected.status_code == 409
                        assert (
                            await asyncio.to_thread(
                                main,
                                [
                                    *target,
                                    "steer",
                                    *instance,
                                    handle.run_id,
                                    "Continue carefully",
                                ],
                            )
                            == 0
                        )
                        assert (
                            await asyncio.to_thread(
                                main,
                                [
                                    *target,
                                    "cancel",
                                    *instance,
                                    handle.run_id,
                                ],
                            )
                            == 0
                        )
                        accepted = await http.get("/api/v0/profile")
                        assert accepted.status_code == 200
                        before = layout.runtime_status.read_bytes()
                        with RuntimeRegistration(
                            layout, toolang_root=tmp_path, launch_id=registration.token
                        ):
                            assert layout.runtime_status.read_bytes() == before
                        assert layout.runtime_status.read_bytes() == before
                        assert (await http.get("/api/v0/profile")).status_code == 200
                    result = await handle
                    assert result.status == "canceled"
            finally:
                gate.release()
                await core.close()
                await harness.close()

    asyncio.run(scenario())
    assert layout.run_store.exists()
    assert not list((tmp_path / ".runtime" / "instances").glob("*.json"))
