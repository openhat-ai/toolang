"""Agent/Hub HTTP parity, lease-fenced publication and cached coverage."""

import asyncio
from contextlib import closing
import json

from fakeredis import FakeServer
from fastapi import FastAPI
import httpx

from toolang.api.routers.activity import router as agent_router, stream as agent_stream
from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution import statistics
from toolang.execution.store import RunStore
from toolang.teaming.api import create_app
from toolang.teaming.activity import HubActivity
from tests.unit.messaging.test_protocol import client
from tests.unit.execution.test_activity import root, model, at, clock


def test_http_sources_share_absolute_stats_and_freeze_offline_cache(
    tmp_path, monkeypatch
):
    async def scenario():
        with closing(RunStore(tmp_path / "runs.db")) as store:
            statistics.start_session(store, "test", at(0))
            root(store)
            model(store)
            reader = ActivityReader(store.db_path, "agent:alice")
            agent = FastAPI()
            agent.state.activity = reader
            agent.include_router(agent_router, prefix="/api/v1")
            original = httpx.AsyncClient
            server = FakeServer(server_type="valkey")
            async with (
                client(server, "human:owner") as human,
                client(server, "agent:alice") as alice,
            ):
                await alice.register("human:owner", endpoint="http://agent")
                hub = create_app(human)
                async with (
                    hub.router.lifespan_context(hub),
                    original(
                        transport=httpx.ASGITransport(agent), base_url="http://agent"
                    ) as local,
                    original(
                        transport=httpx.ASGITransport(hub), base_url="http://hub"
                    ) as http,
                ):
                    # Federation still crosses the agent HTTP routes, using an
                    # in-process transport to keep the default suite offline.
                    monkeypatch.setattr(
                        "toolang.teaming.activity.httpx.AsyncClient",
                        lambda **kwargs: original(
                            transport=httpx.ASGITransport(agent), **kwargs
                        ),
                    )
                    params = {"since": "all", "all_recent": "true"}
                    response = await local.get("/api/v1/activity/batch", params=params)
                    assert response.status_code == 200
                    direct = response.json()[0]
                    response = await http.get("/activity", params=params)
                    assert response.status_code == 200
                    federated = response.json()[0]
                    assert federated["revision"] == direct["revision"]
                    assert federated["stats"]["model"] == direct["stats"]["model"] == 1
                    assert federated["stats"]["cost"] == direct["stats"]["cost"] == 0.5
                    assert (
                        federated["thread_eligible"] == direct["thread_eligible"] == 1
                    )
                    assert federated["thread_matched"] == direct["thread_matched"] == 1
                    assert [n["id"] for n in federated["roots"]] == [
                        n["id"] for n in direct["roots"]
                    ]
                    assert "time_rate" not in direct["stats"]
                    assert (
                        await local.get(
                            "/api/v1/activity", params={"since": "yesterday"}
                        )
                    ).status_code == 422
                    await alice.unregister()
                    cached = await HubActivity(human.backend).read(
                        ActivityQuery("all", None)
                    )
                    assert cached[0].stale and cached[0].presence == "offline"
                    assert cached[0].stats.cost == 0.5
                    assert cached[0].observed == federated["observed"]
                    assert not cached[0].paths
                    unknown = await HubActivity(human.backend).read(
                        ActivityQuery(at(30), None)
                    )
                    assert not unknown[0].complete
                    assert unknown[0].stats.cost is None
                    assert unknown[0].total.cost is None

    asyncio.run(scenario())


def test_publication_fences_old_lease_and_does_not_regress_revision(tmp_path):
    async def scenario():
        with closing(RunStore(tmp_path / "runs.db")) as store:
            statistics.start_session(store, "one", at(0))
            root(store)
            pages = ActivityReader(store.db_path, "agent:alice").pages(
                ActivityQuery(), now=clock(10)
            )
            server = FakeServer(server_type="valkey")
            async with (
                client(server, "human:owner") as human,
                client(server, "agent:alice") as alice,
            ):
                await alice.register("human:owner")
                backend = human.backend.activity
                await backend.save(alice.actor, alice.token, pages)
                old = pages[0].model_copy(update={"revision": 0, "observed": clock(5)})
                await backend.save(alice.actor, alice.token, [old])
                assert (await backend.cached(alice.actor, ActivityQuery()))[
                    0
                ].revision == pages[0].revision
                await alice.unregister()
                newer = pages[0].model_copy(update={"revision": pages[0].revision + 1})
                await backend.save(alice.actor, alice.token, [newer])
                assert (await backend.cached(alice.actor, ActivityQuery()))[
                    0
                ].revision == pages[0].revision

    asyncio.run(scenario())


def test_agent_stream_stages_pages_then_checkpoint_without_token_payloads(tmp_path):
    async def scenario():
        with closing(RunStore(tmp_path / "runs.db")) as store:
            statistics.start_session(store, "one", at(0))
            root(store)
            model(store, status="running")
            reader = ActivityReader(store.db_path, "agent:alice")
            iterator = agent_stream(ActivityQuery(), reader)
            try:
                page = await anext(iterator)
                boundary = await anext(iterator)
                assert page.event == "activity_page"
                assert boundary.event == "activity_checkpoint"
                assert boundary.data == {"agents": ["agent:alice"]}
                assert "given" not in json.dumps(page.data)
                assert "delta" not in json.dumps(page.data)
            finally:
                await iterator.aclose()

    asyncio.run(scenario())


def test_offline_filtered_snapshot_keeps_historical_matches_and_scope_counts(tmp_path):
    async def scenario():
        with closing(RunStore(tmp_path / "runs.db")) as store:
            statistics.start_session(store, "one", at(0))
            root(store)
            model(store)
            root(store, "run_other")
            query = ActivityQuery("all", None, "run_root.0")
            pages = ActivityReader(store.db_path, "agent:alice").pages(
                query, now=clock(100)
            )
            assert pages[0].active == 2 and pages[0].eligible == 2
            assert len(pages[0].roots) == 1
            server = FakeServer(server_type="valkey")
            async with (
                client(server, "human:owner") as human,
                client(server, "agent:alice") as alice,
            ):
                await alice.register("human:owner")
                backend = human.backend.activity
                await backend.save(alice.actor, alice.token, pages)
                await alice.unregister()
                cached = (await HubActivity(human.backend).read(query))[0]
                assert [node.id for node in cached.roots] == ["run_root"]
                assert cached.roots[0].matches == ["run_root.0"]
                assert cached.active == 2 and cached.eligible == 2
                assert cached.threads[0].active == 2
                assert cached.observed == clock(100)

    asyncio.run(scenario())
