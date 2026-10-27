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
        backend.roots = AsyncMock(return_value=["/test/toolang"])
        backend.for_root.return_value = backend
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
        backend.roots = AsyncMock(return_value=["/test/toolang"])
        backend.for_root.return_value = backend
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


def test_lifecycle_reentry_orders_startup_and_cancels_before_closing():
    async def scenario():
        log = []
        backend = Mock()
        backend.ping = AsyncMock(side_effect=lambda: log.append("ping"))
        backend.initialize = AsyncMock(side_effect=lambda: log.append("schema"))
        backend.close = AsyncMock(side_effect=lambda: log.append("close"))

        async def run():
            log.append("presence:start")
            try:
                await asyncio.Future()
            finally:
                log.append("presence:stop")

        lifecycle = HubLifecycle(backend)
        lifecycle.presence = Mock()
        lifecycle.presence.reconcile_once = AsyncMock(
            side_effect=lambda: log.append("presence:scan")
        )
        lifecycle.presence.run = run
        for _ in range(2):
            log.clear()
            async with lifecycle.lifespan(lambda: log.append("ready")):
                assert lifecycle.ready and len(lifecycle.tasks) == 1
                assert log[:2] == ["ping", "presence:scan"]
                assert log[-1] == "ready"
            assert log[-2:] == ["presence:stop", "close"]
            assert all(task.done() for task in lifecycle.tasks)
            assert not lifecycle.ready

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["ping", "scan", "worker", "ready"])
def test_startup_failure_closes_resources_and_all_started_tasks(stage):
    async def scenario():
        backend = Mock()
        backend.ping = AsyncMock(
            side_effect=RuntimeError("ping") if stage == "ping" else None
        )
        backend.close = AsyncMock()
        lifecycle = HubLifecycle(backend)
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
        backend.close.assert_awaited_once()
        assert all(task.done() for task in lifecycle.tasks)
        assert not lifecycle.ready

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
        await human.check_backend()
        app = create_app(human.config, backend=human.backend, version="0.4.0-test")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                headers={"X-Toolang-Root": "%2Ftest%2Ftoolang"},
                transport=httpx.ASGITransport(app),
                base_url="http://hub",
            ) as http,
        ):
            assert (await http.get("/healthz")).status_code == 200
            release.set()
            await finished.wait()
            # Run the worker's done callback, without a wall-clock delay.
            await asyncio.sleep(0)
            assert (await http.get("/healthz")).status_code == 503

    asyncio.run(scenario())
