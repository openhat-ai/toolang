"""Team subscriptions and atomic-write failures on isolated Redis/Valkey servers."""

from toolang.setup.teaming import load_teaming_root

import asyncio
import json
from urllib.parse import quote

import httpx
from httpx_sse import aconnect_sse
import pytest
from valkey.asyncio import Valkey

from tests.integration.messaging.test_valkey import (
    valkey as valkey,
    running_hub as running_hub,
    hub_connection,
)
from toolang.teaming.agent_client import AgentClient
from toolang.teaming.errors import StorageIntegrityError
from toolang.teaming.backend.valkey.keys import Keys
from toolang.teaming.messaging import MessagingClient

pytestmark = pytest.mark.live_valkey


@pytest.mark.parametrize("field", ["dm_count", "messages_total"])
def test_invalid_counter_cannot_partially_commit_on_wire(valkey, field):
    async def scenario():
        async with (
            MessagingClient(valkey, actor="human:owner") as human,
            MessagingClient(valkey, actor="agent:alice") as alice,
            Valkey.from_url(valkey.url, decode_responses=False) as raw,
        ):
            await alice.register(human.actor)
            await raw.hset(Keys(valkey.root_code).stats, field, "0.0")
            before = {key: await raw.dump(key) for key in await raw.keys("*")}
            with pytest.raises(StorageIntegrityError):
                await human.send("alice", body="must not partially commit")
            assert {key: await raw.dump(key) for key in await raw.keys("*")} == before

    asyncio.run(scenario())


@pytest.mark.parametrize("replaced", [False, True])
def test_team_sse_closes_on_expired_or_replaced_agent_lease(
    valkey, running_hub, tmp_path, replaced
):
    async def scenario():
        connection = hub_connection(tmp_path)
        async with (
            AgentClient(
                tmp_path,
                connection=load_teaming_root(tmp_path).connection(root=str(tmp_path)),
                actor="agent:alice",
                token="old",
            ) as alice,
            AgentClient(
                tmp_path,
                connection=load_teaming_root(tmp_path).connection(root=str(tmp_path)),
                actor="agent:alice",
                token="new",
            ) as replacement,
            Valkey.from_url(valkey.url, decode_responses=True) as raw,
            httpx.AsyncClient(
                base_url=connection.endpoint,
                timeout=5,
                trust_env=False,
                headers={"X-Toolang-Root": quote(connection.root, safe="")},
            ) as http,
        ):
            await alice.register("human:owner")
            response = await http.get("/team/events", params={"after": "invalid"})
            assert response.status_code == 400
            async with aconnect_sse(
                http,
                "GET",
                "/agents/agent:alice/team/events",
                headers={"X-Toolang-Agent-Lease": "old"},
            ) as stream:
                stream.response.raise_for_status()
                events = stream.aiter_sse()
                assert (await anext(events)).event == "checkpoint"
                await raw.zadd(Keys(valkey.root_code).presence, {alice.actor: 0})
                if replaced:
                    await replacement.register("human:owner")
                error = await anext(events)
                assert error.event == "stream_error"
                assert json.loads(error.data) == {"code": "recovery_required"}
                with pytest.raises(StopAsyncIteration):
                    await anext(events)

    asyncio.run(scenario())
