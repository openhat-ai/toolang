"""Talk handler cursors belong to agents and are guarded with backend authority."""

import asyncio
from unittest.mock import AsyncMock

from tests.support.talk import admitted_handler
from urllib.parse import quote

from fakeredis import FakeServer
import httpx
import pytest

from tests.unit.messaging.test_protocol import CONFIG, client, snapshot
from tests.unit.messaging.test_agent_api import agent
from tests.unit.messaging.test_loop import make_loop, prepare
from toolang.common.layout import AgentLayout
from toolang.teaming.api import create_app
from toolang.teaming.backend.valkey.keys import (
    PREFIX,
    TEAM_MEMBERS,
    PRESENCE,
    talk_key,
    convo_key,
)
from toolang.teaming.errors import (
    BackendUnavailable,
    ConversationAccessDenied,
    LeaseLost,
    MessagingError,
    StorageIntegrityError,
)
from toolang.teaming.schemas import stream_id


def test_agent_conversation_isolation_and_progress_survives_departure(tmp_path):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
            client(server, "agent:bob") as bob,
        ):
            await prepare(alice, human)
            await bob.register(human.actor)
            convo = (await human.resolve("dev", kind="name")).conversation
            other = (await human.resolve("other", kind="name")).conversation
            await bob.join_conversation(convo)
            sent = await human.send(convo, body="first")
            assert await alice.talk_cursor(convo) == "0-0"
            await alice.advance_talk_cursor(convo, sent["stream_id"])
            assert await bob.talk_cursor(convo) == "0-0"
            assert await alice.talk_cursor(other) == "0-0"
            raw = human.backend._client
            assert await raw.hgetall(talk_key(alice.actor)) == {
                convo: sent["stream_id"]
            }
            assert await raw.ttl(talk_key(alice.actor)) == -1
            assert await raw.exists(PREFIX + ":team") == 0
            assert await raw.hexists(TEAM_MEMBERS, alice.actor)
            await alice.leave_conversation(convo)
            with pytest.raises(ConversationAccessDenied):
                await alice.talk_cursor(convo)
            await alice.join_conversation(convo)
            assert await alice.talk_cursor(convo) == sent["stream_id"]
            await alice.unregister()
            await alice.register(human.actor)
            assert await alice.talk_cursor(convo) == sent["stream_id"]
            with pytest.raises(MessagingError, match="agent"):
                await human.talk_cursor(convo)

    asyncio.run(scenario())


def test_cursor_updates_are_idempotent_and_invalid_writes_do_not_mutate():
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register(human.actor)
            sent = await human.send("alice", body="first")
            convo, cursor = sent["conversation"], sent["stream_id"]
            await alice.advance_talk_cursor(convo, cursor)
            before = await snapshot(human.backend._client)
            await alice.advance_talk_cursor(convo, cursor)
            assert await snapshot(human.backend._client) == before
            ms, seq = stream_id(cursor)
            for value in (
                "0-0",
                f"{ms}-{seq + 1}",
                "not-a-stream-id",
                "01-0",
                f"{2**64}-0",
            ):
                with pytest.raises((MessagingError, ValueError)):
                    await alice.advance_talk_cursor(convo, value)
                assert await snapshot(human.backend._client) == before
            await human.backend._client.zadd(PRESENCE, {alice.actor: 1})
            before = await snapshot(human.backend._client)
            with pytest.raises(LeaseLost):
                await alice.advance_talk_cursor(convo, cursor)
            assert await snapshot(human.backend._client) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("bad", ["broken", "01-0", f"{2**64}-0", f"1-{2**64}"])
def test_corrupt_saved_cursor_fails_without_writes(bad):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register(human.actor)
            sent = await human.send("alice", body="first")
            raw = human.backend._client
            await raw.hset(talk_key(alice.actor), sent["conversation"], bad)
            before = await snapshot(raw)
            for action in (
                lambda: alice.talk_cursor(sent["conversation"]),
                lambda: alice.advance_talk_cursor(
                    sent["conversation"], sent["stream_id"]
                ),
            ):
                with pytest.raises(StorageIntegrityError):
                    await action()
                assert await snapshot(raw) == before

    asyncio.run(scenario())


def test_pending_cursor_retries_only_write_and_ignores_old_files(tmp_path, monkeypatch):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await prepare(alice, human)
            convo = (await human.resolve("dev", kind="name")).conversation
            old = (
                AgentLayout.resident(tmp_path, "alice").runtime
                / "channels/messaging/v2-old.json"
            )
            old.parent.mkdir(parents=True)
            old.write_text("corrupt old JSON")
            sent = await human.send(convo, body="process once")
            loop = make_loop(tmp_path, alice)
            handler = admitted_handler(loop)
            advance = alice.advance_talk_cursor
            attempts = 0

            async def lost_ack(conversation, cursor):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise BackendUnavailable("before acceptance")
                await advance(conversation, cursor)
                if attempts == 2:
                    raise BackendUnavailable("response lost after acceptance")

            monkeypatch.setattr(alice, "advance_talk_cursor", lost_ack)
            await loop.poll()
            with pytest.raises(BackendUnavailable):
                await loop.poll()
            await loop.poll()
            await loop.poll()
            assert handler.await_count == 1 and attempts == 2
            assert await alice.talk_cursor(convo) == sent["stream_id"]
            assert loop.pending is None
            assert old.read_text() == "corrupt old JSON"
            assert list(old.parent.iterdir()) == [old]
            relocated = make_loop(tmp_path / "relocated", alice)
            relocated.handle = AsyncMock()
            await relocated.poll()
            relocated.handle.assert_not_awaited()
            assert not (tmp_path / "relocated/agents/alice/.runtime/channels").exists()

    asyncio.run(scenario())


def test_talk_cursor_hub_api_is_agent_only_and_fenced(tmp_path):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with client(server, "human:owner") as human:
            app = create_app(human, version="0.4.0-test")
            async with (
                agent(app, tmp_path) as alice,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://hub"
                ) as http,
            ):
                await alice.register(human.actor)
                sent = await human.send("alice", body="hello")
                convo = sent["conversation"]
                assert await alice.talk_cursor(convo) == "0-0"
                await alice.advance_talk_cursor(convo, sent["stream_id"])
                assert await alice.talk_cursor(convo) == sent["stream_id"]
                headers = {
                    "X-Toolang-Human": quote(human.actor, safe=""),
                    "X-Toolang-Backend": CONFIG.identity,
                    "X-Toolang-Agent-Lease": "lease",
                }
                route = f"/agents/agent:alice/talk/cursors/{convo}"
                before = await snapshot(human.backend._client)
                response = await http.put(
                    route, headers=headers, json={"cursor": "01-0"}
                )
                assert response.status_code == 400
                assert await snapshot(human.backend._client) == before
                response = await http.put(
                    route,
                    headers={**headers, "X-Toolang-Agent-Lease": "stale"},
                    json={"cursor": sent["stream_id"]},
                )
                assert response.status_code == 409
                assert await snapshot(human.backend._client) == before
                response = await http.get(f"/talk/cursors/{convo}", headers=headers)
                assert response.status_code == 404
                await alice.unregister()
                response = await http.get(route, headers=headers)
                assert response.status_code == 409

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["membership", "lease"])
def test_cursor_authority_is_checked_at_atomic_write(monkeypatch, change):
    from toolang.teaming.backend.valkey import scripts

    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register(human.actor)
            sent = await human.send("alice", body="first")
            raw = alice.backend._client
            evaluate = alice.backend._eval
            changed = False

            async def change_before_write(script, keys, args):
                nonlocal changed
                if script == scripts.TALK_CURSOR and not changed:
                    changed = True
                    if change == "membership":
                        await raw.srem(
                            convo_key(sent["conversation"], "members"), alice.actor
                        )
                    else:
                        await raw.zadd(PRESENCE, {alice.actor: 1})
                return await evaluate(script, keys, args)

            monkeypatch.setattr(alice.backend, "_eval", change_before_write)
            with pytest.raises(
                (ConversationAccessDenied, LeaseLost, StorageIntegrityError)
            ):
                await alice.advance_talk_cursor(sent["conversation"], sent["stream_id"])
            assert not await raw.exists(talk_key(alice.actor))

    asyncio.run(scenario())


def test_cancelled_batch_has_bounded_cursor_save(tmp_path, monkeypatch):
    from toolang.driver import talk as messaging

    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await prepare(alice, human)
            convo = (await human.resolve("dev", kind="name")).conversation
            sent = await human.send(convo, body="cancel")
            loop = make_loop(tmp_path, alice)
            admitted_handler(loop, asyncio.CancelledError)
            monkeypatch.setattr(messaging, "_CURSOR_SAVE_SECONDS", 0.01)

            async def stalled_save(*args):
                await asyncio.Event().wait()

            monkeypatch.setattr(alice, "advance_talk_cursor", stalled_save)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(loop.poll(), 1)
            assert loop.pending == (convo, sent["stream_id"])
            assert not (
                AgentLayout.resident(tmp_path, "alice").runtime / "channels"
            ).exists()

    asyncio.run(scenario())


def test_pending_cursor_is_discarded_after_backend_switch(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.unit.messaging.test_agent_api import CONNECTION
    from toolang.teaming.agent_client import AgentClient
    from toolang.teaming.config import BackendConfig

    async def scenario():
        humans = [
            client(FakeServer(server_type="valkey"), "human:owner") for _ in range(2)
        ]
        for index, human in enumerate(humans):
            human.config = BackendConfig(f"redis://cursor-{index}")
        apps = [create_app(human, version="0.4.0-test") for human in humans]
        current = 0

        async def dispatch(request):
            return await httpx.ASGITransport(apps[current]).handle_async_request(
                request
            )

        async with (
            humans[0],
            humans[1],
            AgentClient(
                tmp_path,
                actor="agent:alice",
                token="lease",
                connection=lambda: replace(
                    CONNECTION, identity=humans[current].config.identity
                ),
                transport=httpx.MockTransport(dispatch),
            ) as alice,
        ):
            await alice.register("human:owner")
            old_convo = (await alice.contacts())[0]["conversation"]
            old = await humans[0].send(old_convo, body="old backend")
            loop = make_loop(tmp_path, alice)

            async def switch_while_handling(batch):
                nonlocal current
                current = 1

            admitted_handler(loop, switch_while_handling)
            await loop.poll()
            assert loop.pending == (old_convo, old["stream_id"])
            await alice.register("human:owner")
            new_convo = (await alice.contacts())[0]["conversation"]
            assert await alice.talk_cursor(new_convo) == "0-0"
            await loop.poll()
            assert loop.pending is None
            assert not await humans[1].backend._client.exists(talk_key(alice.actor))
            admitted_handler(loop)
            await humans[1].send(new_convo, body="new backend")
            await loop.poll()
            loop.handle.assert_awaited_once()

    asyncio.run(scenario())


def test_pending_cursor_recovers_after_another_lease_advances_progress(
    tmp_path, monkeypatch
):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice", token="original") as alice,
            client(server, "agent:alice", token="replacement") as replacement,
        ):
            await prepare(alice, human)
            convo = (await human.resolve("dev", kind="name")).conversation
            first = await human.send(convo, body="handled by original")
            loop = make_loop(tmp_path, alice)
            admitted_handler(loop)
            advance = alice.advance_talk_cursor
            attempts = 0

            async def interrupted_save(conversation, cursor):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise BackendUnavailable("first save was interrupted")
                await advance(conversation, cursor)

            monkeypatch.setattr(alice, "advance_talk_cursor", interrupted_save)
            await loop.poll()
            assert loop.pending == (convo, first["stream_id"])
            await alice.unregister()
            await replacement.register(human.actor)
            second = await human.send(convo, body="handled by replacement")
            await replacement.advance_talk_cursor(convo, second["stream_id"])
            await replacement.unregister()
            await alice.register(human.actor)

            await loop.poll()
            assert loop.pending is None
            assert await alice.talk_cursor(convo) == second["stream_id"]
            await loop.poll()
            assert loop.handle.await_count == 1
            third = await human.send(convo, body="new work after recovery")
            await loop.poll()
            assert loop.handle.await_count == 2
            assert await alice.talk_cursor(convo) == third["stream_id"]

    asyncio.run(scenario())


def test_cursor_authority_loss_does_not_replace_handler_cancellation(tmp_path):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await prepare(alice, human)
            convo = (await human.resolve("dev", kind="name")).conversation
            sent = await human.send(convo, body="cancel during lease loss")
            loop = make_loop(tmp_path, alice)

            async def lose_lease_and_cancel(batch):
                await alice.unregister()
                raise asyncio.CancelledError

            admitted_handler(loop, lose_lease_and_cancel)
            task = asyncio.create_task(loop.poll())
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            assert loop.pending == (convo, sent["stream_id"])
            assert await alice.talk_cursor(convo) == "0-0"

    asyncio.run(scenario())
