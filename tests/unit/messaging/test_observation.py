"""Direct and Hub observations share durable accounting without starting services."""

import asyncio
import sqlite3
from contextlib import aclosing, closing

from fakeredis import FakeServer
import httpx
import pytest

from toolang.execution import statistics
from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution.store import RunStore
from toolang.teaming.api import create_app
from toolang.execution.schemas import ActivitySnapshot
from toolang.teaming.observation import CombinedObservation, LocalObservation, frames
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


def test_combined_updates_keep_sources_independent_and_local_precedence():
    class Source:
        def __init__(self, label):
            self.label = label
            self.batches = asyncio.Queue()
            self.closed = False

        async def read(self, query):
            raise AssertionError("Live observation must subscribe to source updates")

        async def updates(self, query):
            try:
                while True:
                    batch = await self.batches.get()
                    if isinstance(batch, Exception):
                        raise batch
                    for frame in frames(batch):
                        yield frame
            finally:
                self.closed = True

        async def result(self, agent, ref):
            return self.label

    async def scenario():
        remote, local = Source("remote"), Source("local")
        combined = CombinedObservation(remote, local)
        snapshot = ActivitySnapshot(
            agent="agent:alice", revision=1, observed=None, since="session", recent=None
        )
        local.batches.put_nowait([snapshot])

        async def batch(stream):
            pages = []
            async with asyncio.timeout(2):
                async for event, data in stream:
                    if event == "activity_page":
                        pages.append(ActivitySnapshot.model_validate(data))
                    elif event == "activity_checkpoint":
                        return pages

        async with aclosing(combined.updates(ActivityQuery())) as stream:
            assert await batch(stream) == [snapshot]
            remote.batches.put_nowait(
                [
                    snapshot.model_copy(update={"revision": 2}),
                    snapshot.model_copy(update={"agent": "agent:bob"}),
                ]
            )
            pages = await batch(stream)
            assert [(page.agent, page.revision) for page in pages] == [
                ("agent:alice", 1),
                ("agent:bob", 1),
            ]
            assert await combined.result("agent:alice", "run_one") == "local"
            remote.batches.put_nowait(ValueError("Hub unavailable"))
            assert await batch(stream) == [snapshot]
            updated = snapshot.model_copy(update={"revision": 3})
            local.batches.put_nowait([updated])
            assert await batch(stream) == [updated]
            remote.batches.put_nowait([snapshot.model_copy(update={"revision": 2})])
            assert await batch(stream) == [updated]
            local.batches.put_nowait([])
            assert (await batch(stream))[0].revision == 2
            assert await combined.result("agent:alice", "run_one") == "remote"
            remote.batches.put_nowait([])
            assert await batch(stream) == []
            with pytest.raises(ValueError, match="no longer available"):
                await combined.result("agent:alice", "run_one")
        assert remote.closed and local.closed

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
                await human.backend.register(
                    human.actor,
                    agent="agent:alice",
                    token="lease",
                    endpoint="http://unused",
                )
                await human.backend.release_lease("agent:alice", "lease")
                app = create_app(
                    human,
                    version="0.4.0-test",
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
