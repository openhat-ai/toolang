"""Registration, rather than HOME discovery, owns Hub membership."""

import asyncio

from fakeredis import FakeServer
import httpx
import pytest

from tests.unit.messaging.test_protocol import client
from toolang.teaming.agent_client import AgentClient
from toolang.teaming.api import create_app
from toolang.teaming.errors import MessagingError
from toolang.teaming.schemas import HubConnection


def test_registration_keeps_offline_members_and_history_without_home(tmp_path):
    async def scenario():
        (tmp_path / "agents" / "unstarted").mkdir(parents=True)
        human = client(FakeServer(server_type="valkey"), "human:owner")
        app = create_app(human.config, backend=human.backend, version="test")
        async with (
            app.router.lifespan_context(app),
            AgentClient(
                tmp_path,
                actor="agent:alice",
                connection=HubConnection("http://hub"),
                transport=httpx.ASGITransport(app),
            ) as alice,
        ):
            assert await human.backend.participants() == {}
            await alice.register("human:owner")
            assert not (tmp_path / "agents" / "alice").exists()
            await alice.unregister()
            assert not await human.backend.online("agent:alice")
            assert "agent:alice" in await human.agents()
            receipt = await human.send("alice", body="Waiting while offline")
            await alice.register("human:owner")
            rows = await alice.history(receipt["conversation"])
            assert len(rows) == 1
            assert "agent:unstarted" not in await human.agents()

    asyncio.run(scenario())


def test_registration_preserves_owner_and_live_lease_fencing(tmp_path):
    async def scenario():
        human = client(FakeServer(server_type="valkey"), "human:owner")
        app = create_app(human.config, backend=human.backend, version="test")
        async with (
            app.router.lifespan_context(app),
            AgentClient(
                tmp_path,
                actor="agent:alice",
                token="first",
                connection=HubConnection("http://hub"),
                transport=httpx.ASGITransport(app),
            ) as first,
            AgentClient(
                tmp_path,
                actor="agent:alice",
                token="second",
                connection=HubConnection("http://hub"),
                transport=httpx.ASGITransport(app),
            ) as second,
        ):
            await first.register("human:owner")
            with pytest.raises(MessagingError, match="owner mismatch"):
                await second.register("human:other")
            with pytest.raises(MessagingError, match="already online"):
                await second.register("human:owner")
            assert "human:other" not in await human.backend.participants()
            await first.unregister()
            await second.register("human:owner")
            assert await human.backend.online("agent:alice")

    asyncio.run(scenario())
