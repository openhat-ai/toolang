"""Top remains available without the historical statistics service."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from fastapi import FastAPI
import httpx

from tests.support.execution_fixtures import project_run_start
from toolang.api.app import get_agent_core
from toolang.api.routers import stats
from toolang.api.routers import activity as top
from toolang.base.types.message import Message
from toolang.execution.schemas import LiveSnapshot, LiveTreeSnapshot, RunNode
from toolang.execution.store import RunStore
from toolang.execution.types import RunReport
from toolang.stats.types import timestamp
from toolang.up.schemas import ActivitySnapshot

NOW = 1792326877


def test_activity_handoff_pins_before_live_and_scans_after_gate(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs.db")
    project_run_start(
        store,
        run_id="run_top",
        thread_id="chat_top",
        input=Message.user("fixture"),
        started_at=timestamp(NOW - 10),
    )
    root = RunNode("run_top", None, "agic:task", "running", timestamp(NOW - 10))
    gate_held = False
    finished = False

    class Stream:
        @contextmanager
        def gate(self):
            nonlocal gate_held, finished
            gate_held = True
            yield
            gate_held = False
            if not finished:
                store.finish_run(
                    run_id="run_top", finished_at=timestamp(NOW), report=RunReport()
                )
                finished = True

    class Executor:
        stream = Stream()

        def live_trees(self):
            assert gate_held
            return LiveSnapshot(
                "epoch",
                timestamp(NOW),
                () if finished else (LiveTreeSnapshot("run_top", "chat_top", (root,)),),
            )

    app = FastAPI()
    app.include_router(top.router)
    app.include_router(stats.router)
    app.dependency_overrides[get_agent_core] = lambda: SimpleNamespace(
        store=store, executor=Executor()
    )
    monkeypatch.setattr("toolang.up.activity.time.time", lambda: NOW)
    monkeypatch.setattr("toolang.api.routers.stats.time.time", lambda: NOW)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.get("/top/snapshot")
            assert first.status_code == 200
            before = ActivitySnapshot.model_validate(first.json())
            assert before.live is not None
            assert len(before.live.trees) == 1 and before.completed.counts.total == 0
            second = await client.get("/top/snapshot")
            after = ActivitySnapshot.model_validate(second.json())
            assert after.live is not None
            assert not after.live.trees and [
                row.id for row in after.completed.roots
            ] == ["run_top"]
            assert after.completed.counts.total == 1
            assert after.completed.roots[0].thread_id == "chat_top"
            usage = await client.get("/stats/recent?period=28800")
            assert usage.status_code == 200
            assert usage.json()["metrics"]["model_calls"] == 0
            assert (await client.get("/stats/summary")).status_code == 503
            assert (await client.get("/top/snapshot?before_time=x")).status_code == 422
            assert (await client.get("/top/snapshot?recent=901")).status_code == 422
            assert (await client.get("/stats/recent?period=28801")).status_code == 422

    try:
        asyncio.run(scenario())
        assert not (tmp_path / "stats.db").exists()
    finally:
        store.close()
