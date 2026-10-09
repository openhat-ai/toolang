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
from toolang.teaming.messaging import MessagingClient
from toolang.teaming.backend import Backend, group_key
from toolang.teaming.config import BackendConfig, TeamingHomeConfig, TeamingRootConfig
from toolang.plugin.toolsets.msg import MsgToolset
from toolang.plugin.toolsets.loading import tools_from_toolsets
from toolang.plugin.types import LoadedPlugin
from toolang.work.messaging import MessagingLoop

CONFIG = BackendConfig("redis://test")
REPLY_TO = "ad451d5a-7465-4d2d-b0b6-f06490657d5f"


def make_client(server, actor="agent:alice"):
    return MessagingClient(
        CONFIG,
        actor=actor,
        backend=Backend(
            CONFIG, client=FakeAsyncValkey(server=server, decode_responses=True)
        ),
    )


async def prepare(agent, human):
    await agent.register("human:owner")
    for name in ("dev", "other"):
        await human.create_group(name)
        await agent.join_group(f"group:{name}")


def make_loop(path, client, harness=None):
    return MessagingLoop(
        layout=AgentLayout.resident(path, "alice"),
        owner="human:owner",
        client=client,
        executor=harness.executor if harness else MagicMock(),
        threads=harness.threads if harness else MagicMock(),
        get_agent_setup=lambda: harness.setup if harness else MagicMock(),
        get_agent_state=lambda: harness.state if harness else MagicMock(),
    )


def harness_with_msg(path, server, responses, source="agic:\n  {{_}}\n"):
    msg = MsgToolset({"root": str(path)})
    setattr(msg, "connection", lambda context: make_client(server))
    tools = tools_from_toolsets({"msg": LoadedPlugin("msg", "msg", msg, "built-in")})
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
                "msg__send",
                {"target": "group:dev", "body": "done", "in_reply_to": REPLY_TO},
            ),
        )
    )
    harness = harness_with_msg(
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
        async with (
            harness,
            make_client(server) as client,
            make_client(server, "human:owner") as human,
        ):
            await prepare(client, human)
            await human.send("group:dev", body="literal $prompt {{x}} @file")
            loop = make_loop(tmp_path, client, harness)
            await loop.poll()
            saved = loop.saved["group:dev"]
            assert saved["result"]["status"] == "succeeded"
            receipt = saved["result"]["replies"][0]
            handled = harness.store.get_run(run_id=saved["result"]["run"])
            assert handled is not None
            assert receipt["message"]["origin"] == {
                "thread": handled.thread.id,
                "run": saved["result"]["run"],
            }
            assert receipt["message"]["in_reply_to"] == REPLY_TO
            assert len(await client.history("group:dev")) == 2
            await loop.poll()  # Own reply is context, never another model invocation.
            assert len(harness.adapter.invocations) == 2
            await human.send("group:dev", body="next")
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
                len(await client.history("group:dev")) == 3
            )  # Final summaries are not messages.

    asyncio.run(scenario())


def test_failed_batches_skip_bad_entries_and_rotate_groups(tmp_path):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            make_client(server) as client,
            make_client(server, "human:owner") as human,
        ):
            await prepare(client, human)
            await FakeAsyncValkey(server=server, decode_responses=True).xadd(
                group_key("group:dev", "messages"), {"data": "broken"}
            )
            for i in range(25):
                await human.send("group:dev", body=str(i))
            await human.send("group:other", body="other context")
            loop = make_loop(tmp_path, client)
            loop.handle = AsyncMock(side_effect=RuntimeError("handler failed"))
            await loop.poll()
            assert len(loop.saved["group:dev"]["messages"]) == 19
            assert loop.saved["group:dev"]["result"]["status"] == "failed"
            await loop.poll()
            assert loop.handle.call_args[0][0]["groups"][0]["group"] == "group:other"
            assert (
                loop.handle.call_args[0][0]["groups"][0]["previous"]["messages"] == []
            )
            await loop.poll()
            batch = loop.handle.call_args[0][0]["groups"][0]
            assert batch["previous"]["result"]["error"] == "handler failed"
            assert len(loop.saved["group:dev"]["messages"]) == 20
            assert (
                json.loads(loop.path.read_text())["group:dev"]["cursor"]
                == loop.saved["group:dev"]["cursor"]
            )

    asyncio.run(scenario())


def test_arrivals_during_handling_and_cancellation_save_delivered_receipts(tmp_path):
    server = FakeServer(server_type="valkey")
    gate = AsyncGate()
    harness = harness_with_msg(
        tmp_path,
        server,
        [
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "send",
                        "send",
                        "msg__send",
                        {"target": "group:dev", "body": "delivered"},
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
        async with (
            harness,
            make_client(server) as client,
            make_client(server, "human:owner") as human,
        ):
            await prepare(client, human)
            source = await human.send("group:dev", body="please reply")
            loop = make_loop(tmp_path, client, harness)
            task = asyncio.create_task(loop.poll())
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            await human.send("group:dev", body="arrived during handler")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            saved = loop.saved["group:dev"]
            assert saved["cursor"] == source["stream_id"]
            assert saved["result"]["status"] == "cancelled"
            assert len(saved["result"]["replies"]) == 1
            gate.release()

    asyncio.run(scenario())


def test_heartbeat_continues_during_slow_batch_and_reconnects(tmp_path, monkeypatch):
    from toolang.work import teaming
    from toolang.teaming.errors import BackendUnavailable

    async def scenario():
        client = MagicMock()
        client.register = AsyncMock(
            side_effect=[BackendUnavailable("temporarily unavailable"), None, None]
        )
        client.renew = AsyncMock(
            side_effect=[BackendUnavailable("Hub stopped"), None, None, None]
        )
        client.unregister = AsyncMock()
        client.close = AsyncMock()
        loop = make_loop(tmp_path, client)
        entered = asyncio.Event()

        polls = 0

        async def slow_poll():
            nonlocal polls
            polls += 1
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(loop, "poll", slow_poll)
        monkeypatch.setattr(teaming, "RENEW_SECONDS", 0.01)
        drained = asyncio.Event()
        exporter = MagicMock()
        exporter.run = drained.wait
        exporter.finish = drained.set
        monkeypatch.setattr(teaming, "EventExporter", lambda *args, **kwargs: exporter)
        lifecycle = teaming.TeamingLoop(loop, MagicMock())
        lifecycle.start()
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(200):
            if client.renew.await_count >= 2:
                break
            await asyncio.sleep(0.005)
        assert client.renew.await_count >= 2
        assert polls == 1 and lifecycle._consumer is not None
        assert not lifecycle._consumer.done()
        await lifecycle.stop_messages()
        await lifecycle.close()
        assert client.register.await_count == 3
        client.unregister.assert_awaited_once_with()
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
    from toolang.setup.teaming import TeamingSetup

    core.setup.current.return_value.teaming = TeamingSetup(
        TeamingRootConfig("human:owner", CONFIG, 7000), TeamingHomeConfig(enabled)
    )
    scheduler = MagicMock()
    scheduler.start, scheduler.pause, scheduler.stop = (
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )
    entered, stopped = asyncio.Event(), asyncio.Event()

    def message_loop(**kwargs):
        assert (
            kwargs["client"].actor == "agent:alice"
            and kwargs["executor"] is core.executor
        )
        return object()

    class Lifecycle:
        def __init__(self, messaging, publisher, **kwargs):
            pass

        def start(self):
            assert scheduler.start.await_count == 0
            entered.set()

        async def stop_messages(self):
            assert core.close.await_count == 0
            stopped.set()

        async def close(self):
            core.close.assert_awaited_once()

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
    monkeypatch.setattr(server, "TeamingLoop", Lifecycle)
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
    from toolang.teaming.schemas import Message

    server = FakeServer(server_type="valkey")
    harness = harness_with_msg(
        tmp_path,
        server,
        [
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "send",
                        "send",
                        "msg__send",
                        {"target": "group:dev", "body": "Worker result"},
                    ),
                )
            ),
            ModelCallResult(message=ModelMessage.assistant("Sent.")),
        ],
        source="flow parent(_: Text) -> Text:\n  let job = spawn worker\nagic worker(_: Text) -> Text:\n  {{_}}\n",
    )

    async def scenario():
        async with (
            harness,
            make_client(server) as client,
            make_client(server, "human:owner") as human,
        ):
            await prepare(client, human)
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
            entries = await client.history("group:dev")
            assert len(entries) == 1
            message = Message.decode(entries[0][1]["data"])
            assert message.origin is not None and message.origin["run"] != parent.id
            worker = harness.store.get_run(run_id=message.origin["run"])
            assert (
                worker is not None
                and worker.status == "succeeded"
                and worker.thread != parent.thread
                and message.origin["thread"] == worker.thread.id
            )

    asyncio.run(scenario())


def test_msg_is_unavailable_without_configuration_and_fences_offline_sends(tmp_path):
    from toolang.base.types.tool import ToolContext
    from toolang.teaming.errors import MessagingError

    async def scenario():
        context = ToolContext(tmp_path / "alice", tmp_path / "room")
        with pytest.raises(MessagingError, match="disabled"):
            await (
                MsgToolset({})
                .tools()["send"]
                .invoke({"target": "group:all", "body": "hi"}, context)
            )
        server = FakeServer(server_type="valkey")
        async with make_client(server) as agent:
            await agent.register("human:owner")
            await agent.unregister()
        msg = MsgToolset({"root": str(tmp_path)})
        setattr(msg, "connection", lambda context: make_client(server))
        assert set(msg.tools()) == {
            "targets",
            "send",
            "create_group",
            "join_group",
            "leave_group",
        }
        with pytest.raises(MessagingError, match="lease lost"):
            await msg.tools()["send"].invoke(
                {"target": "group:all", "body": "hi"}, context
            )

    asyncio.run(scenario())


def test_all_msg_tools_use_the_context_identity(tmp_path):
    from toolang.base.types.tool import MsgToolContext
    from toolang.teaming.errors import MessagingError

    async def scenario():
        server = FakeServer(server_type="valkey")
        async with make_client(server) as agent:
            await agent.register("human:owner")
        msg = MsgToolset({"root": str(tmp_path)})
        setattr(
            msg,
            "connection",
            lambda context: make_client(server, f"agent:{context.home.name}"),
        )
        context = MsgToolContext(
            tmp_path / "alice",
            tmp_path / "room",
            run_id="run_test",
            thread_id="term_test",
        )
        tools = msg.tools()
        result = await tools["targets"].invoke({}, context)
        assert result.output["participants"][0]["target"] == "human:owner"
        created = await tools["create_group"].invoke({"name": "dev"}, context)
        assert created.output == {"group": "group:dev", "members": ["agent:alice"]}
        sent = await tools["send"].invoke(
            {"target": "group:dev", "body": "hello"}, context
        )
        assert sent.output["message"]["sender"] == "agent:alice"
        assert sent.output["message"]["origin"] == {
            "run": "run_test",
            "thread": "term_test",
        }
        left = await tools["leave_group"].invoke({"group": "group:dev"}, context)
        assert left.output["members"] == []
        joined = await tools["join_group"].invoke({"group": "group:dev"}, context)
        assert joined.output["members"] == ["agent:alice"]
        with pytest.raises(MessagingError, match="Invalid target"):
            await tools["send"].invoke({"target": "dev", "body": "ambiguous"}, context)

    asyncio.run(scenario())
