"""Direct and Hub observations share durable accounting without starting services."""

import asyncio
import sqlite3
from contextlib import aclosing, closing

from fakeredis import FakeServer
import httpx

from toolang.execution import statistics
from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution.store import RunStore
from toolang.teaming.api import create_app
from toolang.teaming.observation import LocalObservation
from tests.support.execution_fixtures import project_run_end
from tests.unit.execution.test_activity import at, clock, model, root
from tests.unit.messaging.test_protocol import client


def test_missing_local_store_is_unavailable_without_creating_files(tmp_path):
    async def scenario():
        path = tmp_path / "missing" / "runs.db"
        source = LocalObservation(
            ActivityReader(path, "agent:alice"), presence=lambda: "offline"
        )
        result = (await source.read(ActivityQuery()))[0]
        assert result.presence == "offline" and result.observed is None
        assert result.stats.model is None and result.stats.cost is None
        async with aclosing(source.updates(ActivityQuery())) as stream:
            event, data = await anext(stream)
            assert event == "activity_page" and not data["complete"]
        assert not path.parent.exists()
        assert not source.reader._publishers

    asyncio.run(scenario())


def test_local_presence_changes_restart_shared_projection_without_execution(tmp_path):
    async def scenario():
        with closing(RunStore(tmp_path / "runs.db")) as store:
            statistics.start_session(store, "one", at(0))
            root(store)
            statistics.checkpoint(store, "one", at(40))
            reader = ActivityReader(store.db_path, "agent:alice")
            online = True
            source = LocalObservation(
                reader, presence=lambda: "online" if online else "offline"
            )
            query = ActivityQuery("session", None)
            async with aclosing(source.updates(query)) as stream:
                event, first = await anext(stream)
                assert event == "activity_page" and first["presence"] == "online"
                online = False
                async with asyncio.timeout(3):
                    while True:
                        event, page = await anext(stream)
                        if event == "activity_page" and page["presence"] == "offline":
                            break
                assert page["stats"]["time"] == 40 and not page["paths"]
                assert len(reader._publishers) == 1
            assert not reader._publishers

    asyncio.run(scenario())


def test_hub_and_local_share_rolling_offline_history_and_results(tmp_path, monkeypatch):
    async def scenario():
        with closing(RunStore(tmp_path / "runs.db")) as store:
            statistics.start_session(store, "one", at(0))
            root(store)
            model(store)
            from toolang.execution.types import Output

            project_run_end(
                store,
                run_id="run_root",
                finished_at=at(120),
                output=Output("# Complete", "_"),
            )
            statistics.checkpoint(store, "one", at(120), end=True)
            reader = ActivityReader(store.db_path, "agent:alice")
            original = reader.pages
            monkeypatch.setattr(
                reader,
                "pages",
                lambda query, **kwargs: original(
                    query, now=clock(135), live=kwargs.get("live")
                ),
            )
            source = LocalObservation(reader, presence=lambda: "offline")
            local = (await source.read(ActivityQuery("60s", None)))[0]
            before = store.db_path.stat().st_mtime_ns
            server = FakeServer(server_type="valkey")
            async with client(server, "human:owner") as human:
                await human._backend.register(
                    human.actor,
                    agent="agent:alice",
                    token="lease",
                    endpoint="http://unused",
                )
                await human._backend.lease("agent:alice", "lease", 0)
                app = create_app(
                    human,
                    local_activity=lambda agent: (
                        reader if agent == reader.agent else None
                    ),
                )
                async with (
                    app.router.lifespan_context(app),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app), base_url="http://hub"
                    ) as http,
                ):
                    response = await http.get(
                        "/activity", params={"since": "60s", "all_recent": "true"}
                    )
                    assert response.status_code == 200
                    page = response.json()[0]
                    assert page["stats"] == local.stats.model_dump()
                    assert page["stats"]["time"] == 45 and page["stats"]["cost"] == 0
                    assert page["presence"] == "offline" and page["observed"] == clock(
                        135
                    )
                    result = await http.get(
                        "/activity/result",
                        params={"agent": "agent:alice", "ref": "run_root"},
                    )
                    assert result.status_code == 200 and result.json() == {
                        "text": "# Complete"
                    }

                    def unavailable(ref):
                        raise sqlite3.DatabaseError("Corrupted store")

                    monkeypatch.setattr(reader, "result", unavailable)
                    result = await http.get(
                        "/activity/result",
                        params={"agent": "agent:alice", "ref": "run_root"},
                    )
                    assert result.status_code == 503
            assert store.db_path.stat().st_mtime_ns == before

    asyncio.run(scenario())


def test_missing_metadata_is_unavailable_for_snapshot_and_stream(tmp_path):
    async def scenario():
        with closing(RunStore(tmp_path / "runs.db")) as store:
            store._conn.execute("DELETE FROM activity_meta")
            store._conn.commit()
            source = LocalObservation(
                ActivityReader(store.db_path, "agent:alice"), presence=lambda: "offline"
            )
            pages = await source.read(ActivityQuery())
            assert pages[0].stats.model is None and not pages[0].complete
            async with aclosing(source.updates(ActivityQuery())) as stream:
                event, page = await anext(stream)
                assert event == "activity_page" and not page["complete"]

    asyncio.run(scenario())
