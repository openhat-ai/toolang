"""Offline acceptance checks for the hosted group messaging loop."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.support.execution_harness import (
    ExecutionHarness,
    AsyncGate,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, ToolResultPart, TextPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.types import ThreadPrefix
from toolang.common.layout import AgentLayout
from toolang.up import server
from toolang.work import messaging
from toolang.plugin.toolsets.coop import CoopToolset
from toolang.plugin.toolsets.loading import tools_from_toolsets
from toolang.plugin.types import LoadedPlugin
from toolang.base.types.tool import ToolContext
from toolang.work.messaging import MessagingConfig, MessagingLoop


class MemoryValkey:
    def __init__(self):
        self.members = {}
        self.streams = {}
        self.closed = False
        self.sequence = 0

    async def execute_command(self, command, key, *args):
        if command == "SMEMBERS":
            assert not args
            return self.members.get(key, set())
        assert command == "SISMEMBER"
        return args[0] in self.members.get(key, set())

    async def xread(self, streams, *, count):
        result = []
        for key, cursor in streams.items():
            entries = [
                item
                for item in self.streams.get(key, [])
                if int(item[0].split("-")[0]) > int(cursor.split("-")[0])
            ][:count]
            if entries:
                result.append((key, entries))
        return result

    async def xadd(self, key, fields, **options):
        self.sequence += 1
        stream_id = f"{self.sequence}-0"
        self.streams.setdefault(key, []).append((stream_id, fields))
        return stream_id

    async def aclose(self):
        self.closed = True

    def join(self, group, *members):
        self.members[f"too:group:{group}:members"] = set(members)

    async def send(self, group, *, sender="bob", body="hello", **extra):
        data = dict(
            id=f"message-{self.sequence + 1}", sender=sender, body=body, **extra
        )
        return await self.xadd(f"too:group:{group}:msg", {"data": json.dumps(data)})


def make_loop(tmp_path, client, *, name="alice", groups=("g_dev",), harness=None):
    return MessagingLoop(
        layout=AgentLayout.resident(tmp_path, name),
        executor=harness.executor if harness else MagicMock(),
        threads=harness.threads if harness else MagicMock(),
        get_agent_setup=lambda: harness.setup if harness else MagicMock(),
        get_agent_state=lambda: harness.state if harness else MagicMock(),
        config=MessagingConfig("redis://localhost", groups),
        client=client,
    )


def tool_turn(name="coop__send", **arguments):
    return ModelCallResult(tool_calls=(ToolCall("call-1", "call-1", name, arguments),))


def make_harness(
    tmp_path, client, *, source="agic:\n  {{_}}\n", responses=(), groups=("g_dev",)
):
    coop = CoopToolset(
        {"url": "redis://localhost", "groups": list(groups)}, client=client
    )
    tools = tools_from_toolsets(
        {"coop": LoadedPlugin("coop", "coop", coop, "built-in")}
    )
    return ExecutionHarness.create(
        tmp_path, source=source, responses=responses, tools=tools
    )


def test_config_is_opt_in_and_validates_group_names():
    assert MessagingConfig.from_config({}) is None
    assert MessagingConfig.from_config(
        {"messaging": {"url": "redis://localhost", "groups": ["g_dev", "g_dev"]}}
    ) == MessagingConfig("redis://localhost", ("g_dev",))
    for group in ("h_alice", "d_alice_bob", "d_a%5Fb_c", "g_d%5Falice%5Fbob"):
        assert MessagingConfig.from_config(
            {"messaging": {"url": "redis://localhost", "groups": [group]}}
        )
    for value in (
        {},
        {"url": "x"},
        {"url": "x", "groups": ["g_a:b"]},
        {"url": "x", "groups": ["d_a_b_c"]},
    ):
        with pytest.raises(ValueError):
            MessagingConfig.from_config({"messaging": value})


@pytest.mark.parametrize("custom", [False, True])
def test_tool_replies_directly_and_next_batch_has_receipt_and_summary(tmp_path, custom):
    client = MemoryValkey()
    source = "agic msg(_: Json):\n  {{_}}\n" if custom else "agic:\n  {{_}}\n"
    harness = make_harness(
        tmp_path,
        client,
        source=source,
        responses=[
            tool_turn(group="g_dev", body="done", in_reply_to="message-1"),
            ModelCallResult(message=Message.assistant("Handled the request.")),
            ModelCallResult(message=Message.assistant("Nothing more to send.")),
        ],
    )

    async def scenario():
        async with harness:
            client.join("g_dev", "alice", "bob")
            raw = {"channel": "chat", "body": "original"}
            await client.send("g_dev", body="literal $prompt {{x}} @file", origin=raw)
            loop = make_loop(tmp_path, client, harness=harness)
            await loop.poll()
            assert len(harness.adapter.invocations) == 2
            assert "literal $prompt {{x}} @file" in repr(
                harness.adapter.invocations[0].call.messages
            )
            saved = loop.saved["g_dev"]
            assert saved["result"]["status"] == "succeeded"
            assert saved["messages"][0]["origin"] == raw
            sent = json.loads(client.streams["too:group:g_dev:msg"][-1][1]["data"])
            assert sent["sender"] == "alice"
            assert sent["body"] == "done"
            assert sent["in_reply_to"] == "message-1"
            assert sent["origin"]["run"] == saved["result"]["run"]
            assert saved["result"]["replies"][0]["id"] == sent["id"]
            await loop.poll()
            assert len(harness.adapter.invocations) == 2
            await client.send("g_dev", body="next")
            restarted = make_loop(tmp_path, client, harness=harness)
            await restarted.poll()
            prompt = repr(harness.adapter.invocations[-1].call.messages)
            assert (
                "Handled the request." in prompt
                and "original" in prompt
                and "next" in prompt
            )
            if not custom:
                assert "coop__send" in prompt

    asyncio.run(scenario())


def test_independent_cursors_and_arrivals_during_a_batch(tmp_path):
    async def scenario():
        client = MemoryValkey()
        client.join("g_dev", "alice", "carol", "bob")
        await client.send("g_dev")
        alice = make_loop(tmp_path, client)
        carol = make_loop(tmp_path, client, name="carol")
        received = []

        async def handle(batch, outcome):
            received.append(deepcopy(batch))
            if len(received) == 1:
                await client.send("g_dev", body="arrived during handling")
            outcome.update(status="succeeded", noted={"g_dev": "first batch handled"})

        alice.handle = handle
        carol.handle = AsyncMock()
        await alice.poll()
        assert alice.saved["g_dev"]["cursor"] == "1-0"
        await carol.poll()
        assert carol.saved["g_dev"]["cursor"] == "2-0"
        await alice.poll()
        assert [m["body"] for m in received[1]["groups"][0]["messages"]] == [
            "arrived during handling"
        ]
        assert (
            received[1]["groups"][0]["previous"]["result"]["noted"]
            == "first batch handled"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("custom", [False, True])
def test_handler_can_route_to_an_idle_group_from_the_available_directory(
    tmp_path, custom
):
    client = MemoryValkey()
    source = "agic msg(_: Json):\n  {{_}}\n" if custom else "agic:\n  {{_}}\n"
    harness = make_harness(
        tmp_path,
        client,
        source=source,
        groups=("g_dev", "h_alice", "g_private"),
        responses=[
            tool_turn(group="h_alice", body="Received the relay"),
            ModelCallResult(message=Message.assistant("Sent a DM.")),
        ],
    )

    async def scenario():
        async with harness:
            client.join("g_dev", "alice", "bob")
            client.join("h_alice", "alice", "owner")
            client.join("g_private", "bob", "private-member")
            await client.send("g_dev", body="Please reply to owner in your human DM")
            loop = make_loop(
                tmp_path,
                client,
                groups=("g_dev", "h_alice", "g_private"),
                harness=harness,
            )
            loop.handle = AsyncMock(wraps=loop.handle)
            await loop.poll()
            batch = loop.handle.call_args.args[0]
            assert batch["available_groups"] == [
                {"group": "g_dev", "members": ["alice", "bob"]},
                {"group": "h_alice", "members": ["alice", "owner"]},
            ]
            assert [item["group"] for item in batch["groups"]] == ["g_dev"]
            prompt = repr(harness.adapter.invocations[0].call.messages)
            assert "available_groups" in prompt and "owner" in prompt
            assert "private-member" not in prompt
            sent = json.loads(client.streams["too:group:h_alice:msg"][0][1]["data"])
            assert sent["body"] == "Received the relay"
            client.join("h_alice", "owner")
            await client.send("g_dev", body="next")
            loop.handle = AsyncMock()
            await loop.poll()
            assert loop.handle.call_args.args[0]["available_groups"] == [
                {"group": "g_dev", "members": ["alice", "bob"]}
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "summary",
    [
        "I sent the reply. No JSON needed.",
        'Explanation.\n```json\n{"replies": [{"group": "g_dev", "body": "never send this"}]}\n```',
        "{broken JSON",
    ],
)
def test_delivery_does_not_parse_or_resend_the_final_output(tmp_path, summary):
    client = MemoryValkey()
    harness = make_harness(
        tmp_path,
        client,
        responses=[
            tool_turn(group="g_dev", body='Literal {braces}, [array], "你好"'),
            ModelCallResult(message=Message.assistant(summary)),
        ],
    )

    async def scenario():
        async with harness:
            client.join("g_dev", "alice", "bob")
            await client.send("g_dev")
            loop = make_loop(tmp_path, client, harness=harness)
            await loop.poll()
            assert loop.saved["g_dev"]["result"]["status"] == "succeeded"
            assert len(client.streams["too:group:g_dev:msg"]) == 2
            sent = json.loads(client.streams["too:group:g_dev:msg"][-1][1]["data"])
            assert sent["body"] == 'Literal {braces}, [array], "你好"'
            await loop.poll()
            assert len(harness.adapter.invocations) == 2

    asyncio.run(scenario())


def test_send_is_delivered_before_run_completion_and_survives_cancellation(tmp_path):
    client = MemoryValkey()
    gate = AsyncGate()
    harness = make_harness(
        tmp_path,
        client,
        responses=[
            tool_turn(group="g_dev", body="Sent immediately"),
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("done")), gate=gate
            ),
        ],
    )

    async def scenario():
        async with harness:
            client.join("g_dev", "alice", "bob")
            await client.send("g_dev")
            loop = make_loop(tmp_path, client, harness=harness)
            task = asyncio.create_task(loop.poll())
            await asyncio.wait_for(gate.wait_until_entered(), 3)
            assert len(client.streams["too:group:g_dev:msg"]) == 2
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (
                loop.saved["g_dev"]["result"]["replies"][0]["body"]
                == "Sent immediately"
            )
            await loop.poll()
            assert len(client.streams["too:group:g_dev:msg"]) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments",
    [
        {"group": "g_dev", "body": ""},
        {"group": "g_bad:name", "body": "bad group"},
        {"group": "g_private", "body": "forbidden"},
        {"group": "g_dev", "body": "spoof", "sender": "owner"},
        {"group": "g_dev", "body": "spoof", "origin": {"run": "other"}},
    ],
)
def test_send_tool_rejects_invalid_or_unauthorized_messages(tmp_path, arguments):
    client = MemoryValkey()
    harness = make_harness(
        tmp_path,
        client,
        responses=[
            tool_turn(**arguments),
            ModelCallResult(message=Message.assistant("Sending failed.")),
        ],
    )

    async def scenario():
        async with harness:
            client.join("g_dev", "alice", "bob")
            client.join("g_private", "bob")
            await client.send("g_dev")
            loop = make_loop(tmp_path, client, harness=harness)
            await loop.poll()
            assert len(client.streams["too:group:g_dev:msg"]) == 1
            assert "too:group:g_private:msg" not in client.streams
            assert loop.saved["g_dev"]["result"]["replies"] == []
            results = [
                step.output.value
                for step in harness.store.list_steps(
                    run_id=loop.saved["g_dev"]["result"]["run"]
                )
                if step.output and isinstance(step.output.value, ToolResultPart)
            ]
            assert len(results) == 1 and results[0].error

    asyncio.run(scenario())


def test_contacts_and_unconfigured_tools(tmp_path):
    async def scenario():
        client = MemoryValkey()
        client.join("h_alice", "owner", "alice")
        context = ToolContext(home=tmp_path / "alice", room=tmp_path)
        coop = CoopToolset(
            {"url": "redis://localhost", "groups": ["h_alice", "g_private"]},
            client=cast(messaging.Valkey, client),
        )
        result = await coop.tools()["contacts"].invoke({}, context)
        assert result.output == {
            "groups": [{"group": "h_alice", "members": ["alice", "owner"]}]
        }
        with pytest.raises(ValueError, match="not configured"):
            await (
                CoopToolset({})
                .tools()["send"]
                .invoke({"group": "h_alice", "body": "hello"}, context)
            )

    asyncio.run(scenario())


def test_spawned_run_sends_with_its_own_origin(tmp_path):
    client = MemoryValkey()
    harness = make_harness(
        tmp_path,
        client,
        source="flow parent(_: Text) -> Text:\n  let job = spawn worker\nagic worker(_: Text) -> Text:\n  {{_}}\n",
        responses=[
            tool_turn(group="g_dev", body="Worker result"),
            ModelCallResult(message=Message.assistant("Sent.")),
        ],
    )

    async def scenario():
        async with harness:
            client.join("g_dev", "alice", "bob")
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
            assert len(client.streams["too:group:g_dev:msg"]) == 1
            message = json.loads(client.streams["too:group:g_dev:msg"][0][1]["data"])
            assert message["origin"]["run"] != parent.id
            worker = harness.store.get_run(run_id=message["origin"]["run"])
            assert worker is not None and worker.status == "succeeded"
            assert worker.thread != parent.thread

    asyncio.run(scenario())


def test_failure_and_malformed_messages_are_skipped_but_context_is_preserved(tmp_path):
    async def scenario():
        client = MemoryValkey()
        client.join("g_dev", "alice", "bob")
        await client.xadd("too:group:g_dev:msg", {"data": "invalid JSON"})
        await client.send("g_dev")
        loop = make_loop(tmp_path, client)
        loop.handle = AsyncMock(side_effect=ValueError("handler failed"))
        await loop.poll()
        assert loop.saved["g_dev"]["cursor"] == "2-0"
        assert loop.saved["g_dev"]["result"]["error"] == "handler failed"
        await loop.poll()
        loop.handle.assert_awaited_once()
        await client.send("g_dev", body="try again")
        loop.handle = AsyncMock()
        await loop.poll()
        batch = loop.handle.call_args.args[0]
        assert batch["groups"][0]["previous"]["result"]["status"] == "failed"

    asyncio.run(scenario())


@pytest.mark.parametrize("joined_target", [False, True])
def test_membership_is_required_to_read_and_send_to_other_groups(
    tmp_path, joined_target
):
    client = MemoryValkey()
    harness = make_harness(
        tmp_path,
        client,
        responses=[
            tool_turn(group="g_other", body="reply"),
            ModelCallResult(message=Message.assistant("Handling complete.")),
        ],
    )

    async def scenario():
        async with harness:
            await client.send("g_dev")
            loop = make_loop(tmp_path, client, harness=harness)
            await loop.poll()
            assert not loop.saved and not harness.adapter.invocations
            client.join("g_dev", "alice", "bob")
            if joined_target:
                client.join("g_other", "alice", "carol")
            await loop.poll()
            assert ("too:group:g_other:msg" in client.streams) == joined_target
            assert len(client.streams["too:group:g_dev:msg"]) == 1
            assert loop.saved["g_dev"]["result"]["replies"] == []

    asyncio.run(scenario())


def test_batch_bounds_rotation_and_conversation_context(tmp_path):
    async def scenario():
        client = MemoryValkey()
        groups = tuple(f"g_{i}" for i in range(6))
        for group in groups:
            client.join(group, "alice", "bob")
            for i in range(40):
                await client.send(group, body=f"{group}-{i}")
        loop = make_loop(tmp_path, client, groups=groups)

        async def handle(batch, outcome):
            outcome.update(
                status="succeeded",
                noted={
                    group["group"]: f"handled {group['group']}"
                    for group in batch["groups"]
                },
                replies=[
                    {"group": group["group"], "body": f"reply {group['group']}"}
                    for group in batch["groups"]
                ],
            )

        loop.handle = AsyncMock(side_effect=handle)
        await loop.poll()
        batch = loop.handle.call_args.args[0]
        assert len(batch["groups"]) == 5
        assert sum(len(g["messages"]) for g in batch["groups"]) == 100
        assert [item["group"] for item in batch["available_groups"]] == list(groups)
        await loop.poll()
        batch = loop.handle.call_args.args[0]
        assert batch["groups"][-1]["group"] == "g_5"
        for group in batch["groups"]:
            assert all(
                m["body"].startswith(group["group"])
                for m in group["previous"]["messages"]
            )
            if result := group["previous"]["result"]:
                assert result["noted"] == f"handled {group['group']}"
                assert [reply["group"] for reply in result["replies"]] == [
                    group["group"]
                ]

    asyncio.run(scenario())


def test_poll_recovers_after_connection_failure_and_closes_on_stop(
    tmp_path, monkeypatch
):
    async def scenario():
        client = MemoryValkey()
        stop = asyncio.Event()
        calls = []

        async def poll(self):
            calls.append(True)
            if len(calls) == 1:
                raise ConnectionError("offline")
            stop.set()

        monkeypatch.setattr(messaging.Valkey, "from_url", lambda *a, **k: client)
        monkeypatch.setattr(MessagingLoop, "poll", poll)
        loop = make_loop(tmp_path, None)
        await asyncio.wait_for(loop.run(stop), 2)
        assert len(calls) == 2 and client.closed

    asyncio.run(scenario())


def test_cancellation_skips_inflight_batch_and_closes_client(tmp_path):
    async def scenario():
        client = MemoryValkey()
        client.join("g_dev", "alice", "bob")
        await client.send("g_dev")
        loop = make_loop(tmp_path, client)
        entered = asyncio.Event()

        async def handle(batch, outcome):
            entered.set()
            await asyncio.Event().wait()

        loop.handle = handle
        task = asyncio.create_task(loop.run(asyncio.Event()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.closed
        assert loop.saved["g_dev"]["result"]["status"] == "cancelled"
        assert loop.saved["g_dev"]["cursor"] == "1-0"

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [False, True])
def test_agent_lifespan_owns_messaging_loop(tmp_path, monkeypatch, enabled):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    if enabled:
        layout.config.write_text(
            '[messaging]\nurl = "redis://localhost"\ngroups = ["h_alice"]\n'
        )
    core = MagicMock()
    core.close = AsyncMock()
    core.state.run = AsyncMock()
    core.setup.run = AsyncMock()
    scheduler = MagicMock()
    scheduler.start = AsyncMock()
    scheduler.pause = AsyncMock()
    scheduler.stop = AsyncMock()
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def run_messaging(stop):
        entered.set()
        await stop.wait()
        stopped.set()

    def message_loop(**kwargs):
        assert kwargs["executor"] is core.executor
        assert kwargs["config"].groups == ("h_alice",)
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
    monkeypatch.setattr(server, "validate_agent_ceiling", MagicMock())
    monkeypatch.setattr(server, "_log_state_loaded", MagicMock())
    monkeypatch.setattr(server, "configure_logging", MagicMock())
    monkeypatch.setattr(server, "_restore_termination_signal_defaults", MagicMock())
    monkeypatch.setattr(server.agents, "write_runtime_state", MagicMock())
    monkeypatch.setattr(server.agents, "stop_runtime_state", MagicMock())
    monkeypatch.setattr(server, "MessagingLoop", message_loop)
    monkeypatch.setattr(server, "create_app", create_app)
    monkeypatch.setattr(server, "_run_uvicorn_app", run_server)
    spec = server.resolve_serve(layout=layout, port=8123)
    assert server.serve(spec, environ={}, sandbox="test") == 0
    core.close.assert_awaited_once()
