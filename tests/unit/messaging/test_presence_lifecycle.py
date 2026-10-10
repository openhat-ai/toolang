"""Hub task ownership, bounded expiry, and readiness under controlled failures."""

import asyncio
from unittest.mock import AsyncMock, Mock

from fakeredis import FakeServer
import httpx
import pytest

from tests.unit.messaging.test_protocol import client
from toolang.teaming.api import create_app
from toolang.teaming.errors import BackendUnavailable, StorageIntegrityError
from toolang.teaming.lifecycle import HubLifecycle
from toolang.teaming.presence import PresenceWorker


def test_presence_worker_drains_batches_and_backs_off_without_busy_waiting():
    async def scenario():
        backend = Mock()
        backend.due_presence = AsyncMock(
            side_effect=[
                list(map(str, range(128))),
                [],
                *[BackendUnavailable("outage") for _ in range(5)],
                [],
            ]
        )
        backend.expire_presence = AsyncMock()
        delays = []

        async def wait(delay):
            delays.append(delay)
            if len(delays) == 8:
                raise asyncio.CancelledError

        worker = PresenceWorker(backend, wait=wait)
        with pytest.raises(asyncio.CancelledError):
            await worker.run()
        assert delays == [0, 1, 1, 2, 4, 5, 5, 1]
        assert backend.expire_presence.await_count == 128

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["scan", "wait"])
def test_worker_cancellation_propagates_through_io_and_wait(phase):
    async def scenario():
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def blocked(*_):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        backend = Mock()
        backend.due_presence = AsyncMock(
            side_effect=blocked if phase == "scan" else None, return_value=[]
        )
        worker = PresenceWorker(backend, wait=blocked)
        task = asyncio.create_task(worker.run())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("with_roster", [False, True])
def test_lifecycle_reentry_orders_startup_and_cancels_before_closing(with_roster):
    async def scenario():
        log = []
        client = Mock()
        client.check_backend = AsyncMock(side_effect=lambda: log.append("schema"))
        client.register_human = AsyncMock(side_effect=lambda: log.append("human"))
        client.close = AsyncMock(side_effect=lambda: log.append("close"))
        roster = Mock() if with_roster else None

        async def run(name):
            log.append(name + ":start")
            try:
                await asyncio.Future()
            finally:
                log.append(name + ":stop")

        if roster:
            roster.scan = AsyncMock(side_effect=lambda: log.append("roster:scan"))
            roster.run = lambda: run("roster")
        lifecycle = HubLifecycle(client, roster=roster)
        lifecycle.presence = Mock()
        lifecycle.presence.reconcile_once = AsyncMock(
            side_effect=lambda: log.append("presence:scan")
        )
        lifecycle.presence.run = lambda: run("presence")
        assert not lifecycle.tasks and not lifecycle.ready
        for _ in range(2):
            log.clear()
            async with lifecycle.lifespan(lambda: log.append("ready")):
                assert lifecycle.ready and not lifecycle.failed
                assert len(lifecycle.tasks) == (2 if with_roster else 1)
                assert log[:3] == ["schema", "human", "presence:scan"]
                assert log[-1] == "ready"
            assert log[-1] == "close"
            assert "presence:stop" in log
            assert all(task.done() for task in lifecycle.tasks)
            assert not lifecycle.ready and not lifecycle.failed
            if roster:
                assert log.index("roster:stop") < log.index("close")

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["schema", "human", "scan", "worker", "ready"])
def test_startup_failure_closes_resources_and_all_started_tasks(stage):
    async def scenario():
        client = Mock()
        client.check_backend = AsyncMock(
            side_effect=RuntimeError("schema") if stage == "schema" else None
        )
        client.register_human = AsyncMock(
            side_effect=RuntimeError("human") if stage == "human" else None
        )
        client.close = AsyncMock()
        lifecycle = HubLifecycle(client)
        lifecycle.presence = Mock()
        lifecycle.presence.reconcile_once = AsyncMock(
            side_effect=RuntimeError("scan") if stage == "scan" else None
        )

        async def run():
            if stage == "worker":
                raise RuntimeError("worker")
            await asyncio.Future()

        lifecycle.presence.run = run

        def ready():
            if stage == "ready":
                raise RuntimeError("ready")

        with pytest.raises(RuntimeError):
            async with lifecycle.lifespan(ready):
                pytest.fail("startup must fail")
        client.close.assert_awaited_once()
        assert all(task.done() for task in lifecycle.tasks)
        assert not lifecycle.ready

    asyncio.run(scenario())


def test_roster_discovery_failure_does_not_disable_presence():
    async def scenario():
        client = Mock()
        client.check_backend = AsyncMock()
        client.register_human = AsyncMock()
        client.close = AsyncMock()
        roster = Mock()
        roster.scan = AsyncMock(side_effect=OSError("home unavailable"))
        started = asyncio.Event()

        async def run():
            started.set()
            await asyncio.Future()

        roster.run = run
        lifecycle = HubLifecycle(client, roster=roster)
        lifecycle.presence = Mock()
        lifecycle.presence.reconcile_once = AsyncMock(return_value=0)
        lifecycle.presence.run = run
        async with lifecycle.lifespan():
            assert lifecycle.ready and started.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", [StorageIntegrityError("corrupt"), RuntimeError("unexpected"), None]
)
def test_failed_presence_worker_marks_hub_unhealthy_until_restart(monkeypatch, failure):
    async def scenario():
        release = asyncio.Event()
        finished = asyncio.Event()

        async def run(_):
            await release.wait()
            finished.set()
            if failure is not None:
                raise failure

        monkeypatch.setattr(PresenceWorker, "run", run)
        human = client(FakeServer(server_type="valkey"), "human:owner")
        app = create_app(human)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://hub"
            ) as http,
        ):
            assert (await http.get("/healthz")).status_code == 200
            release.set()
            await finished.wait()
            # Run the worker's done callback, without a wall-clock delay.
            await asyncio.sleep(0)
            assert (await http.get("/healthz")).status_code == 503

    asyncio.run(scenario())
