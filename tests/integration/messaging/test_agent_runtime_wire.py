"""Real Hub transport with deterministic model turns and fault injection."""

import asyncio
import json
import sys

import httpx
from httpx_sse import aconnect_sse
import pytest
from valkey.asyncio import Valkey

from tests.integration.messaging.test_valkey import (
    valkey as valkey,
    running_hub as running_hub,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message as ModelMessage, TextPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.schemas import StreamFrame
from toolang.execution.types import ThreadPrefix
from toolang.plugin.toolsets.loading import tools_from_toolsets
from toolang.plugin.toolsets.msg import MsgToolset
from toolang.plugin.types import LoadedPlugin
from toolang.teaming.agent_client import AgentClient, AgentEventClient
from toolang.teaming.keys import TEAM, PRESENCE
from toolang.teaming.client import HubClient
from toolang.teaming.events import HubScope
from toolang.teaming.stream_client import HubStreamState
from toolang.work.messaging import MessagingLoop
from toolang.work.teaming import TeamingLoop

pytestmark = pytest.mark.live_valkey


@pytest.mark.parametrize("fault", ["restart", "lease_loss", "backend_switch"])
def test_inflight_message_survives_hub_restart_and_scoped_fanout(
    valkey, running_hub, tmp_path, fault
):
    async def scenario():
        async with HubClient(running_hub.connection()) as directory:
            system = next(
                c["conversation"]
                for c in await directory.contacts()
                if c["name"] == "all"
            )
        gate = AsyncGate()
        msg = MsgToolset({"root": str(tmp_path)})
        tools = tools_from_toolsets(
            {"msg": LoadedPlugin("msg", "msg", msg, "built-in")}
        )
        harness = ExecutionHarness.create(
            tmp_path,
            source="agic msg(_: Json):\n  {{_}}\nflow local(_: Text):\n  let result = Done\n",
            tools=tools,
            responses=[
                ScriptedModelTurn(
                    gate=gate,
                    result=ModelCallResult(
                        tool_calls=(
                            ToolCall(
                                "reply",
                                "reply",
                                "msg__send",
                                {"target": system, "body": "handled once"},
                            ),
                        )
                    ),
                ),
                ModelCallResult(message=ModelMessage.assistant("Handled.")),
            ],
        )
        client = AgentClient(tmp_path, actor="agent:alice")
        loop = MessagingLoop(
            layout=harness.setup.layout,
            owner="human:owner",
            executor=harness.executor,
            threads=harness.threads,
            get_agent_setup=lambda: harness.setup,
            get_agent_state=lambda: harness.state,
            client=client,
        )
        lifecycle = TeamingLoop(loop, AgentEventClient(client))
        connection = running_hub.connection()
        record = running_hub.current()
        assert record is not None

        async def caught_up():
            async with asyncio.timeout(25):
                while lifecycle.exporter.highwater != harness.executor.stream.tail:
                    await asyncio.sleep(0.05)

        async with (
            harness,
            HubClient(connection) as human,
            Valkey.from_url(valkey.url, decode_responses=True) as raw,
            Valkey.from_url(valkey.url + "?db=1", decode_responses=True) as second,
            httpx.AsyncClient(
                base_url=connection.endpoint, timeout=15, trust_env=False
            ) as http,
        ):
            lifecycle.start()
            try:
                async with asyncio.timeout(15):
                    while not lifecycle.exporter.generation:
                        await asyncio.sleep(0.05)
                source = await human.send(system, body="please handle this")
                await asyncio.wait_for(gate.wait_until_entered(), 10)
                runs = harness.store.list_runs()
                assert len(runs) == 1
                active = runs[0]
                checkpoint = loop.path
                await caught_up()

                async def snapshot(scope, state=None):
                    state = state or HubStreamState(scope)
                    state.attach()
                    params = {
                        name: value
                        for name, value in {
                            "agent": scope.agent,
                            "thread": scope.thread,
                            "run": scope.run,
                            "after": state.cursor,
                        }.items()
                        if value is not None
                    }
                    async with aconnect_sse(
                        http, "GET", "/events/stream", params=params
                    ) as events:
                        events.response.raise_for_status()
                        async for event in events.aiter_sse():
                            if not event.data:
                                continue
                            frame = StreamFrame(
                                event.event, json.loads(event.data), event.id or None
                            )
                            assert frame.event != "stream_error", frame.data
                            state.feed(frame)
                            if frame.event == "stream_checkpoint":
                                return state
                    pytest.fail("stream ended without a checkpoint")

                scopes = [
                    HubScope(),
                    HubScope("agent:alice"),
                    HubScope("agent:alice", thread=active.thread.id),
                    HubScope("agent:alice", run=active.id),
                ] * 2
                views = await asyncio.gather(*(snapshot(scope) for scope in scopes))
                assert all(
                    not view.agents[client.actor].complete(active.id) for view in views
                )
                await asyncio.to_thread(running_hub.stop)
                if fault == "lease_loss":
                    await raw.zadd(PRESENCE, {client.actor: 1})
                if fault == "backend_switch":
                    (tmp_path / "config.toml").write_text(
                        f'[teaming]\nhuman = "owner"\n[teaming.backend]\nurl = "{valkey.url}?db=1"\n'
                    )
                local = await harness.executor.run(
                    harness.run_spec(
                        thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                        runnable="flow:local",
                        primary=(TextPart("local"),),
                    )
                )
                assert local.status == "succeeded"
                still_active = harness.store.get_run(run_id=active.id)
                assert still_active is not None and still_active.status == "running"
                await asyncio.to_thread(
                    running_hub.start,
                    [
                        sys.executable,
                        "-m",
                        "toolang.cli.toolang.main",
                        "--root",
                        str(tmp_path),
                        "hub",
                        "serve",
                        "--port",
                        str(record.port),
                    ],
                )
                async with asyncio.timeout(25):
                    current = second if fault == "backend_switch" else raw
                    while (
                        json.loads(
                            await current.execute_command("HGET", TEAM, client.actor)
                            or '{"lease": null}'
                        ).get("lease")
                        or {}
                    ).get("token") != client.token:
                        await asyncio.sleep(0.05)
                gate.release()
                async with asyncio.timeout(15):
                    while True:
                        saved = (
                            json.loads(checkpoint.read_text()).get(system, {})
                            if checkpoint.exists()
                            else {}
                        )
                        if saved.get("result", {}).get("status") == "succeeded":
                            break
                        await asyncio.sleep(0.05)
                assert saved["cursor"] == source["stream_id"]
                assert saved["result"]["run"] == active.id
                if fault == "backend_switch":
                    assert saved["result"]["replies"] == []
                    async with HubClient(running_hub.connection()) as replacement:
                        new_system = next(
                            c["conversation"]
                            for c in await replacement.contacts()
                            if c["name"] == "all"
                        )
                        assert await replacement.history(new_system) == []
                else:
                    assert len(saved["result"]["replies"]) == 1
                    assert saved["result"]["replies"][0]["message"]["origin"] == {
                        "thread": active.thread.id,
                        "run": active.id,
                    }
                    assert len(await human.history(system)) == 2
                await caught_up()
                resumed = await asyncio.gather(
                    *(
                        snapshot(scope, view)
                        for scope, view in zip(scopes, views, strict=True)
                    )
                )
                assert all(
                    view.agents[client.actor].complete(active.id) for view in resumed
                )
                await lifecycle.stop_messages()
                # A confirmed own reply must not trigger another turn.
                await loop.poll()
                assert len(harness.adapter.invocations) == 2
            finally:
                gate.release()
                await lifecycle.stop_messages()
                await harness.executor.stop()
                await lifecycle.close()

    asyncio.run(scenario())
