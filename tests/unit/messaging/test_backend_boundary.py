"""Hub services consume storage contracts without depending on a concrete driver."""

import asyncio
from unittest.mock import call, create_autospec
from uuid import uuid4

import httpx

from toolang.execution.schemas import ActivitySnapshot
from toolang.teaming.api import create_app
from toolang.teaming.backend import ActivityBackend, Backend, EventBackend
from toolang.teaming.config import BackendConfig
from toolang.teaming.messaging import MessagingClient
from toolang.teaming.schemas import Conversation, Message


def test_hub_uses_injected_contracts_for_lifespan_messages_activity_and_events():
    async def scenario():
        backend = create_autospec(Backend, instance=True, spec_set=True)
        backend.activity = create_autospec(
            ActivityBackend, instance=True, spec_set=True
        )
        backend.events = create_autospec(EventBackend, instance=True, spec_set=True)
        backend.due_presence.return_value = []
        backend.participants.return_value = {"agent:alice": {"owner": "human:owner"}}
        backend.activity.lease.return_value = {}
        backend.activity.cached.return_value = [
            ActivitySnapshot(
                agent="agent:alice",
                revision=0,
                observed=None,
                since="session",
                recent=1800,
            )
        ]
        backend.events.online_token.return_value = "lease"
        backend.events.initialize.return_value = {"epoch": "a" * 32}
        backend.events.capture.return_value = ({"epoch": "a" * 32}, {}, {})
        convo = Conversation(
            "gc_00000001",
            "gc",
            ("human:owner",),
            name="Development",
            created_by="human:owner",
            created_at="2026-10-10T00:00:00.000Z",
            updated_at="2026-10-10T00:00:00.000Z",
            revision=1,
        )
        message = Message(str(uuid4()), "human:owner", "Hello")
        backend.contacts.return_value = [(convo, [("1-0", {"data": message.encode()})])]
        client = MessagingClient(
            BackendConfig("redis://unused"), actor="human:owner", backend=backend
        )
        app = create_app(client, version="0.4.0-test")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://hub"
            ) as http,
        ):
            assert (await http.get("/healthz")).status_code == 200
            assert (await client.contacts(include_preview=True))[0]["preview"] == {
                "sender": "human:owner",
                "body": "Hello",
            }
            activity = await http.get("/activity")
            assert activity.status_code == 200
            assert activity.json()[0]["presence"] == "offline"
            response = await http.get(
                "/agents/agent:alice/events/state",
                headers={"X-Toolang-Agent-Lease": "lease"},
            )
            assert response.status_code == 200
            assert response.json() == {"meta": {"epoch": "a" * 32}, "origin": None}
            backend.close.assert_not_awaited()
        backend.initialize.assert_awaited_once()
        assert backend.register.await_args_list
        assert all(
            args == call("human:owner") for args in backend.register.await_args_list
        )
        backend.events.capture.assert_awaited_once_with("agent:alice")
        backend.activity.cached.assert_awaited_once()
        backend.close.assert_awaited_once()

    asyncio.run(scenario())
