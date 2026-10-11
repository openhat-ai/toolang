"""Unsupported history stays unavailable without hiding healthy agents or presence."""

import asyncio
from contextlib import aclosing, closing
import sqlite3

from fakeredis import FakeServer
import httpx
import pytest

from toolang.execution import statistics
from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution.schemas import ActivitySnapshot
from toolang.execution.store import RunStore
from toolang.teaming.activity import HubActivity
from toolang.teaming.api import create_app
from toolang.teaming.observation import LocalObservation
from tests.unit.execution.test_activity import at, model, root
from tests.unit.messaging.test_protocol import client


@pytest.fixture(params=[48, 49, 50, 999])
def unsupported_reader(tmp_path, request):
    directory = tmp_path / "unsupported"
    directory.mkdir()
    path = directory / "runs.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"PRAGMA user_version={request.param}")
    before = path.read_bytes(), path.stat().st_mtime_ns
    yield ActivityReader(path, "agent:alice")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert list(directory.iterdir()) == [path]


@pytest.fixture
def readers(tmp_path, unsupported_reader):
    with closing(RunStore(tmp_path / "healthy.db")) as store:
        statistics.start_session(store, "one", at(0))
        root(store)
        model(store)
        statistics.checkpoint(store, "one", at(120), end=True)
    return {
        unsupported_reader.agent: unsupported_reader,
        "agent:bob": ActivityReader(store.db_path, "agent:bob"),
    }


def assert_unavailable(page, presence):
    assert page.presence == presence
    assert page.observed is None and page.stale and not page.complete
    assert page.coverage == "History unavailable"
    assert not page.roots and not page.paths
    for metrics in (page.stats, page.total):
        assert not metrics.complete and not metrics.tokens_complete
        assert all(
            getattr(metrics, name) is None
            for name in (
                "model",
                "tool",
                "cost",
                "time",
                "input_tokens",
                "cached_tokens",
                "output_tokens",
            )
        )


@pytest.mark.parametrize("presence", ["online", "offline", "unknown"])
@pytest.mark.parametrize("streaming", [False, True])
def test_local_unsupported_history(unsupported_reader, presence, streaming):
    async def scenario():
        source = LocalObservation(unsupported_reader, presence=lambda: presence)
        query = ActivityQuery("all", None, "match", True)
        if streaming:
            async with aclosing(source.updates(query)) as stream:
                async with asyncio.timeout(3):
                    event, data = await anext(stream)
                    assert event == "activity_page"
                    page = ActivitySnapshot.model_validate(data)
                    assert await anext(stream) == (
                        "activity_checkpoint",
                        {"agents": [unsupported_reader.agent]},
                    )
            assert not unsupported_reader._publishers
        else:
            page = (await source.read(query))[0]
        assert_unavailable(page, presence)
        assert (page.since, page.recent, page.filter, page.active_only) == (
            "all",
            None,
            "match",
            True,
        )

    asyncio.run(scenario())


def test_hub_snapshot_isolates_unsupported_history(readers):
    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            for agent in readers:
                await human.backend.register(human.actor, agent=agent, token=agent)
            app = create_app(human, version="test", local_activity=readers.get)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                base_url="http://hub",
            ) as http:
                response = await http.get("/activity")
                assert response.status_code == 200
                pages = {
                    p["agent"]: ActivitySnapshot.model_validate(p)
                    for p in response.json()
                }
                assert_unavailable(pages["agent:alice"], "online")
                assert pages["agent:bob"].stats.model == 1
                assert pages["agent:bob"].stats.cost == 0.5
                assert pages["agent:bob"].observed is not None
                await human.backend.release_lease("agent:alice", "agent:alice")
                response = await http.get("/activity", params={"agent": "agent:alice"})
                assert response.status_code == 200
                assert_unavailable(
                    ActivitySnapshot.model_validate(response.json()[0]), "offline"
                )

    asyncio.run(scenario())


def test_hub_details_reports_unsupported_history_as_unavailable(unsupported_reader):
    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            await human.backend.register(
                human.actor, agent="agent:alice", token="lease"
            )
            app = create_app(
                human, version="test", local_activity=lambda _: unsupported_reader
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                base_url="http://hub",
            ) as http:
                response = await http.get(
                    "/activity/result",
                    params={"agent": "agent:alice", "ref": "run_root"},
                )
                assert response.status_code == 503
                assert response.json() == {"detail": "Result unavailable"}

    asyncio.run(scenario())


def test_hub_live_feed_keeps_unsupported_source_and_healthy_updates(readers):
    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            for agent in readers:
                await human.backend.register(human.actor, agent=agent, token=agent)
            reader = HubActivity(human.backend, local_reader=readers.get)
            pages = {}
            unavailable_updates = 0
            async with aclosing(reader.updates(ActivityQuery())) as stream:
                async with asyncio.timeout(5):
                    while unavailable_updates < 2 or pages.get("agent:bob") is None:
                        event, data = await anext(stream)
                        if event != "activity_page":
                            continue
                        page = ActivitySnapshot.model_validate(data)
                        if (
                            page.agent == "agent:alice"
                            and page.coverage == "History unavailable"
                        ):
                            assert_unavailable(page, "online")
                            unavailable_updates += 1
                        elif page.agent == "agent:bob" and page.observed is not None:
                            assert page.stats.model == 1 and page.stats.cost == 0.5
                            pages[page.agent] = page
            assert not reader._feeds
            assert all(not source._publishers for source in readers.values())

    asyncio.run(scenario())
