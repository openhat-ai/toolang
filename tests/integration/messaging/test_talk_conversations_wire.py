"""Conversation workflows through real Hub HTTP, CLI processes, and terminals."""

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import subprocess
import sys

from httpx_sse import aconnect_sse
import httpx
import pytest
from valkey.asyncio import Valkey

from tests.integration.messaging.test_valkey import (
    valkey as valkey,
    running_hub as running_hub,
)
from tests.support.chat_tui_pty import ChatTuiPtySession
from toolang.teaming.agent_client import AgentClient
from toolang.teaming.client import HubClient
from toolang.teaming.ids import dm_id
from toolang.teaming.keys import CONVOS, PRESENCE, STATS, TEAM_EVENTS, convo_key
from toolang.teaming.schemas import Message

pytestmark = pytest.mark.live_valkey


@asynccontextmanager
async def active_agent(root, actor, owner):
    async with AgentClient(root, actor=actor, managed=False) as client:
        await client.register(owner)

        async def renew():
            while True:
                await asyncio.sleep(3)
                await client.renew()

        heartbeat = asyncio.create_task(renew())
        try:
            yield client
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat


def test_talk_empty_dm_first_send_peer_reply_and_observation(
    valkey, running_hub, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOOLANG_TMUX", "0")
    monkeypatch.setenv("COLUMNS", "180")

    def command(*arguments):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "toolang.cli.toolang.main",
                "--root",
                str(tmp_path),
                "talk",
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def terminal(target):
        return ChatTuiPtySession.start(
            "toolang.cli.toolang.main",
            "--root",
            tmp_path,
            "talk",
            target,
            columns=120,
        )

    async def scenario():
        connection = running_hub.connection()
        async with (
            HubClient(connection) as human,
            active_agent(tmp_path, "agent:alice", connection.human) as alice,
            active_agent(tmp_path, "agent:bob", connection.human) as bob,
            Valkey.from_url(valkey.url, decode_responses=True) as raw,
        ):
            direct = dm_id(connection.human, alice.actor)
            before = {key: await raw.dump(key) for key in (CONVOS, STATS, TEAM_EVENTS)}
            listing = await asyncio.to_thread(command)
            assert listing.returncode == 0, listing.stderr
            assert all(
                word in listing.stdout for word in ("Team", "Convos", "alice", "bob")
            )
            assert direct not in listing.stdout
            missing = await asyncio.to_thread(command, "alice,bob")
            assert (
                missing.returncode != 0 and "No conversation exists" in missing.stderr
            )
            tui = terminal("alice")
            try:
                await asyncio.to_thread(tui.wait_for, "write a message", direct)
                tui.send(b"\x11")
                assert await asyncio.to_thread(tui.wait_for_exit) == 0, tui.output
            finally:
                tui.close()
            assert {key: await raw.dump(key) for key in before} == before

            tui = terminal("alice")
            try:
                await asyncio.to_thread(tui.wait_for, "write a message", direct)
                tui.send(b"first human send\r")
                async with asyncio.timeout(8):
                    while not await raw.exists(convo_key(direct, "messages")):
                        await asyncio.sleep(0.05)
                entries = await human.history(direct)
                assert [Message.decode(row[1]["data"]).body for row in entries] == [
                    "first human send"
                ]
                await alice.send("human:owner", body="peer reply over HTTP")
                await asyncio.to_thread(tui.wait_for, "peer reply over HTTP")
                await human.rename_conversation(direct, "Renamed pair", revision=1)
                await asyncio.to_thread(tui.wait_for, "Renamed pair")
                tui.send(b"\x11")
                assert await asyncio.to_thread(tui.wait_for_exit) == 0, tui.output
            finally:
                tui.close()
            listing = await asyncio.to_thread(command)
            assert direct in listing.stdout and "Renamed pair" in listing.stdout
            await alice.register(connection.human)
            await bob.register(connection.human)
            tui = terminal("bob")
            try:
                peer_direct = dm_id(connection.human, bob.actor)
                await asyncio.to_thread(tui.wait_for, "write a message", peer_direct)
                assert not await raw.hexists(CONVOS, peer_direct)
                receipt = await bob.send("human:owner", body="peer creates this DM")
                await asyncio.to_thread(tui.wait_for, "peer creates this DM")
                assert receipt["conversation"] == peer_direct
                tui.send(b"\x11")
                assert await asyncio.to_thread(tui.wait_for_exit) == 0, tui.output
            finally:
                tui.close()
            exchange = await alice.send("bob", body="agents only")
            observed = exchange["conversation"]
            tui = terminal("alice,bob")
            try:
                await asyncio.to_thread(tui.wait_for, "agents only", observed)
                assert "write a message" not in tui.output
                tui.send(b"observer cannot send\r\x11")
                assert await asyncio.to_thread(tui.wait_for_exit) == 0, tui.output
            finally:
                tui.close()
            assert len(await human.history(observed)) == 1

    asyncio.run(scenario())


def test_open_talk_updates_composer_when_gc_membership_changes(
    running_hub, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOOLANG_TMUX", "0")

    async def scenario():
        connection = running_hub.connection()
        async with (
            HubClient(connection) as human,
            active_agent(tmp_path, "agent:alice", connection.human) as alice,
        ):
            conversation = await alice.create_conversation("Observers")
            tui = ChatTuiPtySession.start(
                "toolang.cli.toolang.main",
                "--root",
                tmp_path,
                "talk",
                conversation.id,
            )
            try:
                await asyncio.to_thread(tui.wait_for, "Observers(1)")
                assert "write a message" not in tui.output
                await human.join_conversation(conversation.id)
                await asyncio.to_thread(tui.wait_for, "Observers(2)", "write a message")
                tui.send(b"joined from another client\r")
                async with asyncio.timeout(5):
                    while not await human.history(conversation.id):
                        await asyncio.sleep(0.05)
                assert len(await human.history(conversation.id)) == 1
                await human.leave_conversation(conversation.id)
                # A fresh marker lets the PTY wait for a render after the leave.
                await alice.rename_conversation(
                    conversation.id, "Observer again", revision=1
                )
                await asyncio.to_thread(tui.wait_for, "Observer again(1)")
                tui.send(b"cannot send after leaving\r\x11")
                assert await asyncio.to_thread(tui.wait_for_exit) == 0, tui.output
                assert len(await human.history(conversation.id)) == 1
            finally:
                tui.close()

    asyncio.run(scenario())


def test_presence_expires_naturally_and_gc_names_remain_nonunique(
    valkey, running_hub, tmp_path
):
    async def scenario():
        connection = running_hub.connection()
        async with (
            HubClient(connection) as human,
            AgentClient(tmp_path, actor="agent:alice", managed=False) as alice,
            Valkey.from_url(valkey.url, decode_responses=True) as raw,
            httpx.AsyncClient(
                base_url=connection.endpoint, timeout=20, trust_env=False
            ) as http,
        ):
            async with aconnect_sse(http, "GET", "/team/events") as stream:
                events = stream.aiter_sse()
                checkpoint = await anext(events)
                assert checkpoint.event == "checkpoint"
                await alice.register(connection.human)
                deadline = int(await raw.zscore(PRESENCE, alice.actor))
                first = await human.create_conversation("Development")
                second = await human.create_conversation("Development")
                assert first.id != second.id
                ambiguous = await http.post(
                    "/msg/resolve", json={"target": "Development", "kind": "name"}
                )
                assert ambiguous.status_code == 400
                assert first.id in ambiguous.text and second.id in ambiguous.text
                receipt = await human.send(first.id, body="retained message")
                await human.send(first.id, body="last message")
                await raw.xtrim(
                    convo_key(first.id, "messages"), maxlen=1, approximate=False
                )
                stats = await human.statistics(first.id)
                assert stats["messages_total"] == 2 and stats["messages_retained"] == 1
                assert stats["last_message_stream_id"] != receipt["stream_id"]
                async with asyncio.timeout(20):
                    async for event in events:
                        if event.event != "change":
                            continue
                        data = json.loads(event.data)
                        if data["type"] == "presence.offline":
                            assert data["payload"]["member"] == alice.actor
                            assert data["payload"]["reason"] == "expired"
                            assert data["payload"]["effective_at_ms"] == deadline
                            assert data["payload"]["observed_at_ms"] >= deadline
                            break
                    else:
                        pytest.fail("Subscription closed before natural expiry")
                assert await raw.zscore(PRESENCE, alice.actor) is None
                assert not next(
                    row for row in await human.team() if row["member"] == alice.actor
                )["online"]
                assert (await http.get("/healthz")).status_code == 200
            async with aconnect_sse(
                http, "GET", "/team/events", params={"after": checkpoint.id}
            ) as replay:
                async with asyncio.timeout(5):
                    async for repeated in replay.aiter_sse():
                        if repeated.id == event.id:
                            assert repeated.data == event.data
                            break
                    else:
                        pytest.fail("A second subscriber missed the expiry event")
            await raw.xtrim(TEAM_EVENTS, maxlen=1, approximate=False)
            async with aconnect_sse(
                http, "GET", "/team/events", params={"after": checkpoint.id}
            ) as expired:
                frames = [frame async for frame in expired.aiter_sse()]
                assert [frame.event for frame in frames] == ["resync_required"]

    asyncio.run(scenario())
