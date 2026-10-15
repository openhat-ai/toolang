"""CLI acquisition of one AgentServer-backed run client."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from toolang.execution.remote import RemoteRunClient
from toolang.up.types import AgentServerRef
from toolang.up.lifecycle import LAUNCH_HEADER

from .remote_runtime import inspect_remote_runtime


@asynccontextmanager
async def acquire_run_client(server: AgentServerRef) -> AsyncIterator[RemoteRunClient]:
    """Connect to the acquired agent without constructing an embedded executor."""

    async def check_retention(_request: httpx.Request) -> None:
        if server.check_retention is not None:
            server.check_retention()

    async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as http:
        if server.launch_id is not None:
            http.headers[LAUNCH_HEADER] = server.launch_id
        if server.check_retention is not None:
            http.event_hooks["request"].append(check_retention)
        client = RemoteRunClient(
            server.endpoint,
            client=http,
            client_id=server.client_id,
            guarded=server.launch_id is not None,
        )
        await client.connect()
        try:
            await inspect_remote_runtime(
                http,
                client.endpoint,
                expected_sandbox=server.sandbox,
            )
            yield client
        finally:
            await client.disconnect()
