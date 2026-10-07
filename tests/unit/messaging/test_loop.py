"""Agent batching with real execution and the shared transport."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from fakeredis import FakeAsyncValkey, FakeServer
import pytest

from tests.support.execution_harness import (
    ExecutionHarness,
    AsyncGate,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message as ModelMessage
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.common.layout import AgentLayout
from toolang.messaging.client import MessagingClient, group_key
from toolang.messaging.config import MessagingConfig
from toolang.plugin.toolsets.coop import CoopToolset
from toolang.plugin.toolsets.loading import tools_from_toolsets
from toolang.plugin.types import LoadedPlugin
from toolang.work.messaging import MessagingLoop

CONFIG = MessagingConfig("redis://test", ("gc_dev", "gc_other"))


def make_client(server):
    return MessagingClient(
        CONFIG, client=FakeAsyncValkey(server=server, decode_responses=True)
    )


def make_loop(path, client, harness=None):
    return MessagingLoop(
        layout=AgentLayout.resident(path, "alice"),
        owner="owner",
        config=CONFIG,
        client=client,
        executor=harness.executor if harness else MagicMock(),
        threads=harness.threads if harness else MagicMock(),
        get_agent_setup=lambda: harness.setup if harness else MagicMock(),
        get_agent_state=lambda: harness.state if harness else MagicMock(),
    )


def harness_with_coop(path, server, responses, source="agic:\n  {{_}}\n"):
    coop = CoopToolset({"url": CONFIG.url, "groups": list(CONFIG.groups)})
    setattr(coop, "connection", lambda: make_client(server))
    tools = tools_from_toolsets(
        {"coop": LoadedPlugin("coop", "coop", coop, "built-in")}
    )
    return ExecutionHarness.create(
        path, source=source, responses=responses, tools=tools
    )


@pytest.mark.parametrize("custom", [False, True])
def test_tool_reply_receipt_context_and_restart(tmp_path, custom):
    server = FakeServer(server_type="valkey")
    reply = ModelCallResult(
        tool_calls=(
            ToolCall(
                "call-1",
                "call-1",
                "coop__send",
                {"group": "gc_dev", "body": "done", "in_reply_to": "request"},
            ),
        )
    )
    harness = harness_with_coop(
        tmp_path,
        server,
        [
            reply,
            ModelCallResult(message=ModelMessage.assistant("Handled.")),
            ModelCallResult(message=ModelMessage.assistant("Noted.")),
        ],
        source="agic msg(_: Json):\n  {{_}}\n" if custom else "agic:\n  {{_}}\n",
    )

    async def scenario():
        async with harness, make_client(server) as client:
            await client.register("alice", "owner", "token")
            await client.send(
                "gc_dev", sender="owner", body="literal $prompt {{x}} @file"
            )
            loop = make_loop(tmp_path, client, harness)
            await loop.poll()
            saved = loop.saved["gc_dev"]
            assert saved["result"]["status"] == "succeeded"
            receipt = saved["result"]["replies"][0]
            assert receipt["message"]["origin"] == {
                "agent": "alice",
                "run": saved["result"]["run"],
            }
            assert receipt["message"]["in_reply_to"] == "request"
            assert len(await client.history("gc_dev")) == 2
            await loop.poll()  # Own reply is context, never another model invocation.
            assert len(harness.adapter.invocations) == 2
            await client.send("gc_dev", sender="owner", body="next")
            restarted = make_loop(tmp_path, client, harness)
            restarted.load()
            await restarted.poll()
            prompt = repr(harness.adapter.invocations[-1].call.messages)
            assert (
                "Handled." in prompt
                and "literal $prompt" in prompt
                and "next" in prompt
            )
            assert (
                len(await client.history("gc_dev")) == 3
            )  # Final summaries are not messages.

    asyncio.run(scenario())


def test_failed_batches_skip_bad_entries_and_rotate_groups(tmp_path):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with make_client(server) as client:
            await client.register("alice", "owner", "token")
            await client.redis.xadd(group_key("gc_dev", "msg"), {"data": "broken"})
            for i in range(25):
                await client.send("gc_dev", sender="owner", body=str(i))
            await client.send("gc_other", sender="owner", body="other context")
            loop = make_loop(tmp_path, client)
            loop.handle = AsyncMock(side_effect=RuntimeError("handler failed"))
            await loop.poll()
            assert len(loop.saved["gc_dev"]["messages"]) == 19
            assert loop.saved["gc_dev"]["result"]["status"] == "failed"
            await loop.poll()
            assert loop.handle.call_args[0][0]["groups"][0]["group"] == "gc_other"
            assert (
                loop.handle.call_args[0][0]["groups"][0]["previous"]["messages"] == []
            )
            await loop.poll()
            batch = loop.handle.call_args[0][0]["groups"][0]
            assert batch["previous"]["result"]["error"] == "handler failed"
            assert len(loop.saved["gc_dev"]["messages"]) == 20
            assert (
                json.loads(loop.path.read_text())["gc_dev"]["cursor"]
                == loop.saved["gc_dev"]["cursor"]
            )

    asyncio.run(scenario())


def test_arrivals_during_handling_and_cancellation_save_delivered_receipts(tmp_path):
    server = FakeServer(server_type="valkey")
    gate = AsyncGate()
    harness = harness_with_coop(
        tmp_path,
        server,
        [
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "send",
                        "send",
                        "coop__send",
                        {"group": "gc_dev", "body": "delivered"},
                    ),
                )
            ),
            ScriptedModelTurn(
                result=ModelCallResult(message=ModelMessage.assistant("summary")),
                gate=gate,
            ),
        ],
    )

    async def scenario():
        async with harness, make_client(server) as client:
            await client.register("alice", "owner", "token")
            source = await client.send("gc_dev", sender="owner", body="please reply")
            loop = make_loop(tmp_path, client, harness)
            task = asyncio.create_task(loop.poll())
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            await client.send("gc_dev", sender="owner", body="arrived during handler")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            saved = loop.saved["gc_dev"]
            assert saved["cursor"] == source["stream_id"]
            assert saved["result"]["status"] == "cancelled"
            assert len(saved["result"]["replies"]) == 1
            gate.release()

    asyncio.run(scenario())


def test_heartbeat_continues_during_slow_batch_and_reconnects(tmp_path, monkeypatch):
    from toolang.work import messaging
    from valkey.exceptions import ConnectionError

    async def scenario():
        client = MagicMock()
        client.register = AsyncMock(
            side_effect=[ConnectionError("temporarily unavailable"), None]
        )
        client.renew = AsyncMock()
        client.unregister = AsyncMock()
        client.close = AsyncMock()
        loop = make_loop(tmp_path, client)
        entered = asyncio.Event()

        async def slow_poll():
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(loop, "poll", slow_poll)
        monkeypatch.setattr(messaging, "RENEW_SECONDS", 0.01)
        task = asyncio.create_task(loop.run(asyncio.Event()))
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(100):
            if client.renew.await_count >= 2:
                break
            await asyncio.sleep(0.005)
        assert client.renew.await_count >= 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.register.await_count == 2
        client.unregister.assert_awaited_once_with("alice", loop.token)
        client.close.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [False, True])
def test_hosted_lifespan_starts_and_stops_messaging(tmp_path, monkeypatch, enabled):
    from types import SimpleNamespace
    from toolang.up import server

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    core = MagicMock()
    core.close = AsyncMock()
    core.state.run = AsyncMock()
    core.setup.run = AsyncMock()
    from toolang.setup.messaging import MessagingSetup

    core.setup.current.return_value.messaging = MessagingSetup(
        CONFIG if enabled else None, "owner"
    )
    scheduler = MagicMock()
    scheduler.start, scheduler.pause, scheduler.stop = (
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def run_messaging(stop):
        entered.set()
        await stop.wait()
        stopped.set()

    def message_loop(**kwargs):
        assert kwargs["config"] == CONFIG and kwargs["executor"] is core.executor
        return SimpleNamespace(run=run_messaging)

    def create_app(*args, lifespan, **kwargs):
        return SimpleNamespace(state=SimpleNamespace(), lifespan=lifespan)

    def run_server(app, **kwargs):
        async def scenario():
            async with app.lifespan(app):
                if enabled:
                    await asyncio.wait_for(entered.wait(), 1)
            assert stopped.is_set() == enabled

        asyncio.run(scenario())

    monkeypatch.setattr(server, "AgentCore", lambda *a, **k: core)
    monkeypatch.setattr(server, "JobScheduler", lambda **k: scheduler)
    monkeypatch.setattr(server, "_refresh_core", AsyncMock())
    for name in (
        "validate_agent_ceiling",
        "_log_state_loaded",
        "configure_logging",
        "_restore_termination_signal_defaults",
    ):
        monkeypatch.setattr(server, name, MagicMock())
    monkeypatch.setattr(server.agents, "write_runtime_state", MagicMock())
    monkeypatch.setattr(server.agents, "stop_runtime_state", MagicMock())
    monkeypatch.setattr(server, "MessagingLoop", message_loop)
    monkeypatch.setattr(server, "create_app", create_app)
    monkeypatch.setattr(server, "_run_uvicorn_app", run_server)
    assert (
        server.serve(
            server.resolve_serve(layout=layout, port=8123), environ={}, sandbox="test"
        )
        == 0
    )
    core.close.assert_awaited_once()


def test_spawned_worker_sends_with_its_own_run_origin(tmp_path):
    from toolang.base.types.message import TextPart
    from toolang.execution.types import ThreadPrefix
    from toolang.messaging.schemas import Message

    server = FakeServer(server_type="valkey")
    harness = harness_with_coop(
        tmp_path,
        server,
        [
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "send",
                        "send",
                        "coop__send",
                        {"group": "gc_dev", "body": "Worker result"},
                    ),
                )
            ),
            ModelCallResult(message=ModelMessage.assistant("Sent.")),
        ],
        source="flow parent(_: Text) -> Text:\n  let job = spawn worker\nagic worker(_: Text) -> Text:\n  {{_}}\n",
    )

    async def scenario():
        async with harness, make_client(server) as client:
            await client.register("alice", "owner", "token")
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="flow:parent",
                    primary=(TextPart("Do work"),),
                )
            )
            while harness.executor._tasks:
                await asyncio.gather(*tuple(harness.executor._tasks))
            assert parent.status == "succeeded"
            entries = await client.history("gc_dev")
            assert len(entries) == 1
            message = Message.decode(entries[0][1]["data"])
            assert message.origin is not None and message.origin["run"] != parent.id
            worker = harness.store.get_run(run_id=message.origin["run"])
            assert (
                worker is not None
                and worker.status == "succeeded"
                and worker.thread != parent.thread
            )

    asyncio.run(scenario())


def test_coop_is_unavailable_without_configuration_or_online_agent(tmp_path):
    from toolang.base.types.tool import ToolContext
    from toolang.messaging.errors import MessagingError

    async def scenario():
        context = ToolContext(tmp_path / "alice", tmp_path / "room")
        with pytest.raises(MessagingError, match="not configured"):
            await (
                CoopToolset({})
                .tools()["send"]
                .invoke({"group": "all", "body": "hi"}, context)
            )
        server = FakeServer(server_type="valkey")
        coop = CoopToolset({"url": CONFIG.url, "groups": []})
        setattr(coop, "connection", lambda: make_client(server))
        with pytest.raises(MessagingError, match="not online"):
            await coop.tools()["contacts"].invoke({}, context)

    asyncio.run(scenario())
