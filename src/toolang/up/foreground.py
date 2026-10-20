"""Serve the shared Agent API beside a foreground workload on its event loop."""

import asyncio
from contextlib import asynccontextmanager, nullcontext
import socket
from typing import AsyncIterator

import uvicorn

from toolang.api.app import create_app
from toolang.catalog import CapsManager, ChoresManager
from .core import AgentCore
from .instance import patch_workload
from .observation import RuntimeRegistration


class _ForegroundServer(uvicorn.Server):
    def capture_signals(self):
        # asyncio.run owns the foreground caller's signal/cancellation policy.
        return nullcontext()


@asynccontextmanager
async def foreground_api(
    core: AgentCore, registration: RuntimeRegistration
) -> AsyncIterator[None]:
    """Keep the reserved socket until the API and all admitted work stop."""
    app = create_app(core, CapsManager(core.layout), ChoresManager(core.layout))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = _ForegroundServer(
            uvicorn.Config(
                app, log_config=None, access_log=False, timeout_graceful_shutdown=5
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("foreground Agent API failed to start")
                    await asyncio.sleep(0.01)
            patch_workload(
                core.layout.runtime_status,
                registration.token,
                {
                    "endpoint": endpoint,
                    "api_version": 0,
                    "lifetime": "foreground",
                    "resource_counts": core.resource_counts(),
                },
            )
            if core.lifecycle is not None:
                core.lifecycle.ready()
            yield
        finally:
            if core.lifecycle is not None:
                core.lifecycle.closing = True
            app.state.subscriptions.stop()
            server.should_exit = True
            await task
