from __future__ import annotations

import pytest

from toolang.base.types.model import ModelOverride
from toolang.cli.toolang.commands import thread


def test_rerun_model_option_uses_the_shared_model_body() -> None:
    assert thread._rerun_model_override(
        model_body="test/model effort=4096",
    ) == ModelOverride(identity="test/model", effort=4096)
    assert thread._rerun_model_override(model_body="effort=high") == ModelOverride(
        effort="high"
    )
    assert thread._rerun_model_override(model_body=None) is None


def test_rerun_model_option_rejects_model_removal() -> None:
    with pytest.raises(ValueError, match="does not accept unset"):
        thread._rerun_model_override(model_body="unset")
    with pytest.raises(ValueError, match="was removed"):
        thread._rerun_model_override(model_body="none")


@pytest.mark.parametrize("kind", ["retry", "rerun"])
@pytest.mark.parametrize("show_progress", [False, True])
def test_restart_uses_shared_terminal_surfaces(
    tmp_path, monkeypatch, kind, show_progress
):
    import asyncio
    from contextlib import asynccontextmanager
    from io import StringIO

    from toolang.cli.common.terminal_surfaces import LIGHT_TERMINAL_SURFACES
    from toolang.up.types import AgentServerRef
    from toolang.common.layout import AgentLayout

    class ReachedRun(Exception):
        pass

    environ = {"TOOLANG_COLOR_SCHEME": "light"}
    stream = StringIO()
    monkeypatch.setattr(thread.sys, "stderr", stream)
    monkeypatch.setattr(thread, "load_runtime_environ", lambda *args, **kwargs: environ)
    resolutions = []
    original = thread.resolve_terminal_surfaces

    def resolve(**kwargs):
        assert kwargs == {"environment": environ, "output_stream": stream}
        resolutions.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(thread, "resolve_terminal_surfaces", resolve)

    class Client:
        async def retry(self, request, *, tracer):
            assert len(resolutions) == int(show_progress)
            if show_progress:
                assert tracer.console.surfaces == LIGHT_TERMINAL_SURFACES
            else:
                assert tracer is None
            raise ReachedRun

        rerun = retry

    @asynccontextmanager
    async def acquire(*args, **kwargs):
        yield Client()

    monkeypatch.setattr(thread, "acquire_run_client", acquire)
    with pytest.raises(ReachedRun):
        asyncio.run(
            thread._execute_retry_or_rerun(
                layout=AgentLayout.resident(tmp_path, "alice"),
                server=AgentServerRef(
                    sandbox="host", endpoint="http://runtime.test:7001"
                ),
                kind=kind,
                source="run_source",
                anchor=None,
                commands=(),
                model_override=None,
                show_progress=show_progress,
            )
        )


@pytest.mark.parametrize("kind", ["retry", "rerun"])
@pytest.mark.parametrize("failure", ["stream", "detail"])
def test_restart_observation_failure_does_not_cancel_accepted_work(
    tmp_path, monkeypatch, kind, failure
):
    import asyncio
    from contextlib import asynccontextmanager
    import json

    import httpx

    from toolang.common.layout import AgentLayout
    from toolang.execution.events import RunBegin, RunEnd, RunEvent, run_event_to_data
    from toolang.execution.remote import RemoteRunClient, RemoteRunClientError
    from toolang.execution.types import ControlRef
    from toolang.up.types import AgentServerRef

    run_id = "run_source" if kind == "retry" else "run_new"
    events: list[RunEvent] = [
        RunBegin(
            run=run_id,
            control=ControlRef.for_run(run_id, 0),
            runnable="agic:chat",
            started_at="2026-10-08T00:00:00Z",
        )
    ]
    if failure == "detail":
        events.append(
            RunEnd(
                run=run_id,
                status="succeeded",
                finished_at="2026-10-08T00:00:01Z",
            )
        )
    stream = "".join(
        f"event: {event.type}\ndata: {json.dumps(run_event_to_data(event))}\n\n"
        for event in events
    ).encode()
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/stream"):
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "X-Toolang-Run-ID": run_id,
                },
                content=stream,
            )
        return httpx.Response(503, json={"detail": "temporarily unavailable"})

    @asynccontextmanager
    async def acquire(server):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = RemoteRunClient(server.endpoint, client=http)
            await client.connect()
            try:
                yield client
            finally:
                await client.disconnect()

    monkeypatch.setattr(thread, "acquire_run_client", acquire)
    monkeypatch.setattr(thread, "load_runtime_environ", lambda *_a, **_kw: {})
    with pytest.raises(RemoteRunClientError) as error:
        asyncio.run(
            thread._execute_retry_or_rerun(
                layout=AgentLayout.resident(tmp_path, "alice"),
                server=AgentServerRef(sandbox="host", endpoint="http://runtime.test"),
                kind=kind,
                source="run_source",
                anchor=None,
                commands=(),
                model_override=None,
                show_progress=False,
            )
        )
    assert requests == [
        ("POST", f"/api/v1/runs/run_source/{kind}/stream"),
        *([("GET", f"/api/v1/runs/{run_id}")] if failure == "detail" else []),
    ]
    assert run_id in str(error.value)


@pytest.mark.parametrize("kind", ["retry", "rerun"])
def test_explicit_restart_interruption_still_cancels_accepted_work(
    tmp_path, monkeypatch, kind
):
    import asyncio
    from contextlib import asynccontextmanager

    from toolang.common.layout import AgentLayout
    from toolang.up.types import AgentServerRef

    canceled = []

    class Handle:
        run_id = "run_accepted"

        async def wait(self):
            if not canceled:
                raise asyncio.CancelledError
            return None

    class Client:
        async def retry(self, request, *, tracer):
            return Handle()

        rerun = retry

        async def cancel(self, run_id, **kwargs):
            canceled.append(run_id)

    @asynccontextmanager
    async def acquire(server):
        yield Client()

    monkeypatch.setattr(thread, "acquire_run_client", acquire)
    monkeypatch.setattr(thread, "load_runtime_environ", lambda *_a, **_kw: {})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            thread._execute_retry_or_rerun(
                layout=AgentLayout.resident(tmp_path, "alice"),
                server=AgentServerRef(sandbox="host", endpoint="http://runtime.test"),
                kind=kind,
                source="run_source",
                anchor=None,
                commands=(),
                model_override=None,
                show_progress=False,
            )
        )
    assert canceled == ["run_accepted"]
