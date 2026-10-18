"""The live endpoint and remote client share the resident snapshot contract."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from toolang.api.app import get_agent_core
from toolang.api.routers.runs import router
from toolang.execution.remote import RemoteRunClient, RemoteRunClientError
from toolang.execution.schemas import LiveSnapshot, LiveTreeSnapshot, RunNode


def test_live_http_and_remote_use_only_live_executor_interface():
    snapshot = LiveSnapshot(
        "epoch-a",
        "now",
        (
            LiveTreeSnapshot(
                "run_root",
                "chat_test",
                (RunNode("run_root", None, "agic:task", "running", "now"),),
            ),
        ),
    )
    calls = []
    stopped = False

    class Executor:
        def live_trees(self, *, root_run_id=None):
            calls.append(root_run_id)
            if stopped:
                raise RuntimeError("run executor is stopped")
            if root_run_id == "invalid":
                raise ValueError("invalid run id")
            return (
                snapshot
                if root_run_id in (None, "run_root")
                else LiveSnapshot("epoch-a", "now", ())
            )

    app = FastAPI()
    app.include_router(router, prefix="/api/v0")
    # A core with no Store/history intentionally cannot service detail reads.
    app.dependency_overrides[get_agent_core] = lambda: SimpleNamespace(
        executor=Executor()
    )

    async def scenario():
        nonlocal stopped
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            client = RemoteRunClient("http://test", client=http)
            await client.connect()
            assert await client.live_trees() == snapshot
            assert await client.live_trees(root_run_id="run_root") == snapshot
            assert not (await client.live_trees(root_run_id="run_absent")).trees
            assert calls == [None, "run_root", "run_absent"]
            assert (
                await http.get("/api/v0/runs/live?root_run_id=invalid")
            ).status_code == 422
            stopped = True
            assert (await http.get("/api/v0/runs/live")).status_code == 503
            with pytest.raises(RemoteRunClientError):
                await client.live_trees()
            await client.disconnect()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload", [{}, {"epoch": "x", "observed_at": "now", "trees": [{"bad": True}]}]
)
def test_remote_live_rejects_invalid_data_without_detail_fallback(payload):
    requests = []

    async def response(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=payload)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
            client = RemoteRunClient("http://test", client=http)
            await client.connect()
            with pytest.raises(RemoteRunClientError, match="invalid snapshot"):
                await client.live_trees()
            assert requests == ["/api/v0/runs/live"]

    asyncio.run(scenario())
