"""Process-owned, read-only observation of an embedded executor."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
import logging
import socket

import uvicorn

from toolang.api.app import create_observation_app
from toolang.execution.executor import RunExecutor

from .observation import RuntimeRegistration

_LOGGER = logging.getLogger(__name__)
_STARTUP_TIMEOUT = 3
_SHUTDOWN_TIMEOUT = 3


class _ObservationServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = asyncio.Event()
        self.closed = False

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The script owns process signals and cancellation.
        yield

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        self.ready.set()

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self.closed = True
        await super().shutdown(sockets=sockets)

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            await super().serve(sockets=sockets)
        finally:
            self.ready.set()
            # Uvicorn's regular loop only shuts down after a normal return.
            if self.started and not self.closed:
                await self.shutdown(sockets=sockets)


def _publish(
    registration: RuntimeRegistration,
    endpoint: str | None,
    *,
    error: str | None = None,
) -> None:
    try:
        registration.publish_observation(endpoint, error=error)
    except (OSError, ValueError, RuntimeError):
        # Observation cannot change the script's execution outcome.
        _LOGGER.warning("Could not publish embedded observation", exc_info=True)


@asynccontextmanager
async def observe_embedded(
    executor: RunExecutor, registration: RuntimeRegistration
) -> AsyncIterator[None]:
    """Observe an already-started executor until its owned work has drained."""
    listener: socket.socket | None = None
    server: _ObservationServer | None = None
    task: asyncio.Task[None] | None = None
    stopping = False
    try:
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.setblocking(False)
            endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
            server = _ObservationServer(
                uvicorn.Config(
                    create_observation_app(executor.store.db_path, executor),
                    log_config=None,
                    access_log=False,
                    lifespan="off",
                    ws="none",
                    timeout_graceful_shutdown=1,
                )
            )

            async def serve() -> None:
                assert server is not None and listener is not None
                try:
                    await server.serve(sockets=[listener])
                    if not stopping:
                        raise RuntimeError("embedded observation listener stopped")
                except asyncio.CancelledError:
                    if not stopping:
                        _publish(
                            registration,
                            None,
                            error="embedded observation listener canceled",
                        )
                    raise
                except Exception as exc:
                    _publish(registration, None, error=str(exc) or type(exc).__name__)
                finally:
                    server.ready.set()

            task = asyncio.create_task(serve(), name="embedded-observation")
            await asyncio.wait_for(server.ready.wait(), _STARTUP_TIMEOUT)
            if server.started and not task.done():
                _publish(registration, endpoint)
        except Exception as exc:
            _publish(registration, None, error=str(exc) or type(exc).__name__)
        yield
    finally:
        stopping = True
        try:
            if server is not None:
                server.should_exit = True
            if task is not None:
                try:
                    await asyncio.wait_for(task, _SHUTDOWN_TIMEOUT)
                except TimeoutError:
                    pass
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
        finally:
            if listener is not None:
                listener.close()
            _publish(registration, None)
