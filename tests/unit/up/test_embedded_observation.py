"""Embedded observation uses the executing process's actual LiveTree."""

import asyncio
from contextlib import closing
import socket

import httpx
import pytest

from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult, ModelUsage, ToolCall

from tests.support.execution_harness import AsyncGate, ExecutionHarness, RecordingTool
from toolang.up.embedded import observe_embedded
from toolang.up import embedded
from toolang.up.activity import activity_snapshot
from toolang.up.instance import read_instance
from toolang.up.observation import RuntimeRegistration, runtime_inventory
from toolang.up.top import TopObserver
from toolang.common.layout import AgentLayout
from toolang.execution.store import RunStore
from toolang.execution.types import ThreadKind
from toolang.execution.schemas import RunNode


@pytest.mark.parametrize("launch", ["direct", "run", "async run", "spawn", "parallel"])
def test_embedded_live_roots_and_completion_are_observable(tmp_path, launch):
    async def scenario():
        gate = AsyncGate()
        tool = RecordingTool("fixture__wait", output={"ok": True}, gate=gate)
        async with ExecutionHarness.create(
            tmp_path,
            source={
                "direct": "agic main(_: Text) -> Text:\n  user: {{_}}\n",
                "run": "flow main(_: Text) -> Text:\n  run child\n",
                "async run": "flow main(_: Text) -> Text:\n  let job = async run child\n  await job\n",
                "spawn": "flow main(_: Text) -> Text:\n  let job = spawn child\n  await job\n",
                "parallel": "flow main(_: Text) -> Text[]:\n  generate 2 in 2 lanes using child\n",
            }[launch]
            + "agic child(_: Text) -> Text:\n  user: {{_}}\n",
            responses=[
                *[
                    ModelCallResult(
                        tool_calls=(ToolCall("call", "call", tool.name, {}),),
                        usage=ModelUsage(
                            12, 3, reported_cost=0, reported_currency="USD"
                        ),
                    )
                    for _ in range(2 if launch == "parallel" else 1)
                ],
                *[
                    ModelCallResult(message=Message.assistant("done"))
                    for _ in range(2 if launch == "parallel" else 1)
                ],
            ],
            tools={tool.name: tool},
        ) as harness:
            layout = harness.setup.layout
            harness.executor.start()
            idle_layout = AgentLayout.resident(tmp_path, "idle")
            with (
                closing(RunStore(idle_layout.run_store)),
                RuntimeRegistration(
                    idle_layout, toolang_root=tmp_path, drivers=frozenset()
                ),
                RuntimeRegistration(
                    layout, toolang_root=tmp_path, drivers=frozenset()
                ) as registration,
            ):
                async with observe_embedded(harness.executor, registration):
                    binding = runtime_inventory(tmp_path).sources[0]
                    assert binding.endpoint is not None
                    endpoint = binding.endpoint
                    async with httpx.AsyncClient(trust_env=False) as client:
                        idle = await client.get(endpoint + "/api/v0/top/snapshot")
                        assert idle.status_code == 200
                        assert idle.json()["live"]["trees"] == []
                        for path in ("/docs", "/openapi.json", "/api/v0/runs"):
                            assert (
                                await client.get(endpoint + path)
                            ).status_code == 404
                        assert (
                            await client.post(endpoint + "/api/v0/top/snapshot")
                        ).status_code == 405
                        assert (
                            await client.get(
                                endpoint + "/api/v0/top/snapshot?recent=901"
                            )
                        ).status_code == 422
                        thread = harness.threads.create(kind=ThreadKind.EXEC)
                        handle = harness.executor.run(
                            harness.run_spec(
                                thread=thread,
                                runnable="agic:main",
                                primary=(TextPart("work"),),
                            )
                            if launch == "direct"
                            else harness.run_spec(
                                thread=thread,
                                runnable="flow:main",
                                primary=(TextPart("work"),),
                            )
                        )
                        pending = activity_snapshot(
                            harness.store.db_path, executor=harness.executor, recent=300
                        )
                        assert pending.live is not None
                        assert pending.live.trees[0].nodes[0].status == "pending"
                        await asyncio.wait_for(gate.wait_until_entered(), 5)
                        with closing(
                            TopObserver(
                                tmp_path, agent=None, period=3600, recent=300, refresh=1
                            )
                        ) as observer:
                            snapshot = await asyncio.to_thread(observer.once)
                            assert snapshot.summary.active == (
                                2 if launch == "spawn" else 1
                            )
                            assert snapshot.summary.running == 2
                            live = snapshot.sources[0].live
                            assert live is not None
                            assert len(live.trees) == (2 if launch == "spawn" else 1)
                            tree = next(
                                tree
                                for tree in live.trees
                                if tree.root_run_id == handle.run_id
                            )
                            assert tree.root_run_id == handle.run_id
                            assert tree.thread_id == str(thread)
                            nodes = [node for tree in live.trees for node in tree.nodes]
                            runs = [node for node in nodes if isinstance(node, RunNode)]
                            owner = next(node for node in runs if node.calls.tool_calls)
                            expected_calls = 2 if launch == "parallel" else 1
                            assert owner.calls.model_calls == expected_calls
                            assert owner.calls.tool_calls == expected_calls
                            assert owner.usage is not None
                            assert owner.usage.input_tokens == 12 * expected_calls
                            assert len(runs) == (
                                1
                                if launch == "direct"
                                else 3
                                if launch == "parallel"
                                else 2
                            )
                            assert any(
                                getattr(node, "name", "") == "fixture__wait"
                                for node in nodes
                            )
                            gate.release()
                            assert (await handle).status == "succeeded"
                            finished = await client.get(
                                endpoint + "/api/v0/top/snapshot"
                            )
                            data = finished.json()
                            assert data["live"]["trees"] == []
                            assert data["completed"]["counts"]["succeeded"] == (
                                2 if launch == "spawn" else 1
                            )
                            completed_ids = [
                                root["id"] for root in data["completed"]["roots"]
                            ]
                            assert handle.run_id in completed_ids
                            assert len(completed_ids) == len(set(completed_ids))
                port = httpx.URL(endpoint).port
                with socket.socket() as probe:
                    assert probe.connect_ex(("127.0.0.1", port)) != 0

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["startup", "serve"])
def test_listener_failure_keeps_history_readable_and_active_unknown(
    tmp_path, monkeypatch, failure
):
    failed = asyncio.Event()
    fail_now = asyncio.Event()

    async def fail(*args, **kwargs):
        if failure == "serve":
            await fail_now.wait()
        failed.set()
        raise RuntimeError("observation fixture failed")

    monkeypatch.setattr(
        embedded._ObservationServer,
        "startup" if failure == "startup" else "main_loop",
        fail,
    )

    async def scenario():
        async with ExecutionHarness.create(
            tmp_path, source="flow main(_: Text) -> Text:\n  pass\n", responses=[]
        ) as harness:
            harness.executor.start()
            with RuntimeRegistration(
                harness.setup.layout, toolang_root=tmp_path, drivers=frozenset()
            ) as registration:
                async with observe_embedded(harness.executor, registration):
                    fail_now.set()
                    await asyncio.wait_for(failed.wait(), 5)
                    # The listener publishes after its shutdown has drained.
                    task = next(
                        (
                            task
                            for task in asyncio.all_tasks()
                            if task.get_name() == "embedded-observation"
                        ),
                        None,
                    )
                    if task is not None:
                        await asyncio.wait_for(task, 5)
                    inventory = runtime_inventory(tmp_path)
                    binding = inventory.sources[0]
                    assert binding.endpoint is None
                    assert binding.observation_error == "observation fixture failed"
                    with closing(
                        TopObserver(
                            tmp_path, agent=None, period=3600, recent=300, refresh=1
                        )
                    ) as observer:
                        snapshot = await asyncio.to_thread(observer.once)
                        assert snapshot.summary.active is None
                        assert snapshot.summary.completed is not None
                        assert snapshot.summary.completed.total == 0
                        assert snapshot.summary.usage is not None
                        assert snapshot.summary.usage.model_calls == 0
                        assert (
                            snapshot.sources[0].live_error
                            == "observation fixture failed"
                        )
                    thread = harness.threads.create(kind=ThreadKind.EXEC)
                    result = await harness.executor.run(
                        harness.run_spec(
                            thread=thread,
                            runnable="flow:main",
                            primary=(TextPart("work"),),
                        )
                    )
                    assert result.status == "succeeded", (
                        harness.store.resolve_error(result.error)
                        if result.error
                        else None
                    )

    asyncio.run(scenario())


def test_observation_cancellation_closes_socket_and_preserves_cancellation(tmp_path):
    ready = asyncio.Event()
    endpoint = []

    async def scenario():
        async with ExecutionHarness.create(
            tmp_path, source="flow main(_: Text) -> Text:\n  pass\n", responses=[]
        ) as harness:
            harness.executor.start()
            with RuntimeRegistration(
                harness.setup.layout, toolang_root=tmp_path, drivers=frozenset()
            ) as registration:

                async def observe():
                    async with observe_embedded(harness.executor, registration):
                        endpoint.append(runtime_inventory(tmp_path).sources[0].endpoint)
                        ready.set()
                        await asyncio.Future()

                task = asyncio.create_task(observe())
                await asyncio.wait_for(ready.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                entry = read_instance(harness.setup.layout.runtime_status)
                assert entry is not None
                assert entry["control"]["observation_endpoint"] is None
                with socket.socket() as probe:
                    assert (
                        probe.connect_ex(("127.0.0.1", httpx.URL(endpoint[0]).port))
                        != 0
                    )
                assert not any(
                    task.get_name() == "embedded-observation"
                    for task in asyncio.all_tasks()
                )

    asyncio.run(scenario())
