"""Server retention with real admission, execution, and guarded controls."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.api.app import create_app
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.catalog import CapsManager, ChoresManager
from toolang.execution.types import ThreadPrefix
from toolang.execution.remote import RemoteRunClient
from toolang.up.core import AgentCore
from toolang.up.lifecycle import LAUNCH_HEADER, ServerLifecycle


def test_lost_clients_retain_pending_execution_and_stale_controls_fail(tmp_path):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat(_: Text):\n  {{_}}\n",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("done")), gate=gate
            ),
        ],
    )
    harness.store.close()
    lifecycle = ServerLifecycle("launch", "automatic")
    core = AgentCore(harness.setup.layout, lifecycle=lifecycle)
    core.setup = SimpleNamespace(current=lambda: harness.setup)
    core.state = SimpleNamespace(
        current=lambda: harness.state, load=lambda _revision: harness.state
    )
    app = create_app(
        core,
        CapsManager(core.layout),
        ChoresManager(core.layout),
        cors_allowed_origins=(),
    )

    async def scenario():
        lifecycle.ready()
        lifecycle.lease("launch", "client")
        thread = core.threads.create(prefix=ThreadPrefix.CHAT)
        handle = core.executor.run(
            harness.run_spec(thread=thread, runnable="agic:chat", named={"_": "hello"})
        )
        lifecycle.release("launch", "client")
        assert not lifecycle.elect_exit(core.executor.has_owned_work)
        await asyncio.wait_for(gate.wait_until_entered(), 2)
        record = core.store.get_run(run_id=handle.run_id)
        assert record is not None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://server"
        ) as client:
            for generation in (b"old", b"\xff"):
                wrong_generation = await client.post(
                    "/api/v1/threads",
                    json={},
                    headers=[(LAUNCH_HEADER.encode(), generation)],
                )
                assert wrong_generation.status_code == 409
            assert len(core.store.list_threads()) == 1
            stale = await client.post(
                f"/api/v1/runs/{handle.run_id}/cancel",
                json={"expected_control": f"{handle.run_id}@99"},
            )
            assert stale.status_code == 409
            current = core.store.get_run(run_id=handle.run_id)
            assert current is not None and current.status == "running"
            guarded = RemoteRunClient("http://server", client=client, guarded=True)
            await guarded.connect()
            try:
                steered = await guarded.steer(handle.run_id, Message.user("revise"))
                assert steered.kind == "steer"
                canceled = await guarded.cancel(handle.run_id)
                assert canceled.kind == "cancel"
            finally:
                await guarded.disconnect()
        assert (await asyncio.wait_for(handle, 2)).status == "canceled"
        await asyncio.sleep(0)
        assert lifecycle.elect_exit(core.executor.has_owned_work)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://server"
        ) as client:
            rejected = await client.post("/api/v1/threads", json={})
            assert rejected.status_code == 409
        await core.close()

    asyncio.run(scenario())


def test_independent_spawn_retains_server_after_source_finishes(tmp_path):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Text) -> Text:
  spawn child
agic child(_: Text) -> Text:
  {{_}}
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("done")), gate=gate
            )
        ],
    )
    lifecycle = ServerLifecycle("launch", "automatic")
    harness.executor._admission = lifecycle.admit

    async def scenario():
        async with harness:
            lifecycle.ready()
            lifecycle.lease("launch", "client")
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.CHAT),
                    runnable="flow:parent",
                    primary=(TextPart("input"),),
                )
            )
            assert parent.status == "succeeded"
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            lifecycle.release("launch", "client")
            assert not lifecycle.elect_exit(harness.executor.has_owned_work)
            gate.release()
            await asyncio.wait_for(
                asyncio.gather(
                    *(root.task for root in tuple(harness.executor._root_runs.values()))
                ),
                2,
            )
            await asyncio.sleep(0)
            assert lifecycle.elect_exit(harness.executor.has_owned_work)

    asyncio.run(scenario())


def test_finished_history_is_not_live_ownership_for_a_guarded_control(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="flow ready(_: Text) -> Text:\n  let result = Done\n",
        responses=[],
    )
    lifecycle = ServerLifecycle("launch", "automatic")
    harness.executor._admission = lifecycle.admit

    async def scenario():
        async with harness:
            lifecycle.ready()
            lifecycle.lease("launch", "client")
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.CHAT),
                    runnable="flow:ready",
                    primary=(TextPart("input"),),
                )
            )
            result = await handle
            assert result.status == "succeeded", (
                harness.store.resolve_error(result.error) if result.error else None
            )
            record = harness.store.get_run(run_id=handle.run_id)
            assert record is not None
            with pytest.raises(ValueError, match="live execution owner"):
                harness.executor.cancel(
                    run_id=record.id, expected_control=record.control
                )
            lifecycle.release("launch", "client")
            await asyncio.sleep(0)
            assert lifecycle.elect_exit(harness.executor.has_owned_work)

    asyncio.run(scenario())
