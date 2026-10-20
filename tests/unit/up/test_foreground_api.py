import asyncio

import httpx

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.cli.toolang.main import main
from toolang.up.core import AgentCore
from toolang.execution.types import ThreadKind
from toolang.state.types import StateSyncResult
from toolang.up.foreground import foreground_api
from toolang.up.identity import allocate_roaming
from toolang.up.instance import read_instance
from toolang.up.lifecycle import LAUNCH_HEADER, ServerLifecycle
from toolang.up.observation import RuntimeRegistration, runtime_inventory


def test_shared_foreground_api_observes_and_cancels_the_owning_executor(
    tmp_path, monkeypatch
):
    gate = AsyncGate()
    layout = allocate_roaming(tmp_path / "alice.too", toolang_root=tmp_path)
    instance_id = layout.instance_id
    assert instance_id is not None
    harness = ExecutionHarness.create(
        layout.root,
        source="agic work():\n  Do work.\n",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("done")), gate=gate
            )
        ],
    )

    async def scenario():
        with RuntimeRegistration(
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
                                    str(layout.source_path),
                                    "steer",
                                    "-i",
                                    instance_id,
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
                                    str(layout.source_path),
                                    "cancel",
                                    "-i",
                                    instance_id,
                                    handle.run_id,
                                ],
                            )
                            == 0
                        )
                        accepted = await http.get("/api/v0/profile")
                        assert accepted.status_code == 200
                    result = await handle
                    assert result.status == "canceled"
            finally:
                gate.release()
                await core.close()
                await harness.close()

    asyncio.run(scenario())
    assert layout.run_store.exists()
    assert not list((tmp_path / ".runtime" / "instances").glob("*.json"))
