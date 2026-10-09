"""Opt-in event subscriptions over real Hub sockets on both supported engines."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys

import httpx
from httpx_sse import aconnect_sse
import pytest

from tests.integration.messaging.test_valkey import (
    valkey as valkey,
    running_hub as running_hub,
)
from tests.support.execution_harness import ExecutionHarness
from tests.support.chat_tui_pty import ChatTuiPtySession
from toolang.base.types.message import TextPart
from toolang.execution.schemas import StreamFrame
from toolang.execution.types import ThreadPrefix
from toolang.teaming.backend import Backend
from toolang.teaming.event_backend import EventBackend
from toolang.teaming.errors import EventRecoveryRequired
from toolang.teaming.events import HubScope
from toolang.teaming.exporter import EventExporter
from toolang.teaming.stream_client import HubStreamState

pytestmark = pytest.mark.live_valkey


def test_hub_event_fanout_recovery_and_top_once(
    valkey, running_hub, tmp_path, monkeypatch
):
    async def scenario():
        connection = running_hub.connection()
        harness = ExecutionHarness.create(
            tmp_path / "execution",
            source="flow example:\n  let result = Done\n",
            responses=[],
        )
        driver = Backend(valkey)
        async with (
            harness,
            httpx.AsyncClient(
                base_url=connection.endpoint,
                headers={"Authorization": f"Bearer {connection.token}"},
                timeout=10,
                trust_env=False,
            ) as http,
        ):
            await driver.register("human:owner", agent="agent:alice", token="lease")
            service = EventBackend(driver)
            exporter = EventExporter(
                harness.executor.stream,
                harness.store.db_path,
                service,
                agent="agent:alice",
                token="lease",
            )

            async def snapshot(after=None, run=None):
                params = {}
                if after is not None:
                    params["after"] = after
                scope = HubScope()
                if run is not None:
                    params.update(agent="agent:alice", run=run)
                    scope = HubScope("agent:alice", run=run)
                state = HubStreamState(scope)
                frames = []
                async with aconnect_sse(
                    http, "GET", "/events/stream", params=params
                ) as stream:
                    if not stream.response.is_success:
                        await stream.response.aread()
                        pytest.fail(stream.response.text)
                    stream.response.raise_for_status()
                    async for event in stream.aiter_sse():
                        if not event.data:
                            continue
                        frame = StreamFrame(
                            event.event, json.loads(event.data), event.id or None
                        )
                        assert frame.event != "stream_error", frame.data
                        state.feed(frame)
                        frames.append(frame)
                        if frame.event == "stream_checkpoint" and run is None:
                            break
                return state, frames

            try:
                await exporter.recover("initial")
                initial, _ = await snapshot()
                thread = harness.threads.create(prefix=ThreadPrefix.TERM)
                record = await harness.executor.run(
                    harness.run_spec(
                        thread=thread, runnable="example", primary=(TextPart("input"),)
                    )
                )
                while exporter.highwater.seq < harness.executor.stream.tail.seq:
                    batch = await exporter.reader.receive()
                    for frame in batch.events:
                        await exporter.publish(frame)
                views = await asyncio.gather(
                    *(snapshot(initial.cursor) for _ in range(8))
                )
                assert all(view.cursor == views[0][0].cursor for view, _ in views)
                assert all(
                    view.agents["agent:alice"].complete(record.id) for view, _ in views
                )
                state, frames = await snapshot(initial.cursor, record.id)
                assert state.agents["agent:alice"].complete(record.id)
                assert frames[-1].event == "stream_checkpoint"
                with pytest.raises(EventRecoveryRequired, match="lease"):
                    await service.abandon(
                        "agent:alice", exporter.generation, token="stale"
                    )
                await service.abandon("agent:alice", exporter.generation, token="lease")
                assert await service.projection("agent:alice", exporter.generation)
                # Recover on an existing connection. SSE decoders retain the
                # previous event ID on the unacknowledged replacement frames.
                watching = views[0][0]
                watching.attach()
                async with aconnect_sse(
                    http,
                    "GET",
                    "/events/stream",
                    params={"after": watching.cursor},
                ) as stream:
                    stream.response.raise_for_status()
                    events = stream.aiter_sse()
                    for recovery in (False, True):
                        before = watching.cursor
                        if recovery:
                            await exporter.recover("source_gap")
                        saw_prefill = False
                        async for event in events:
                            if not event.data:
                                continue
                            frame = StreamFrame(
                                event.event, json.loads(event.data), event.id or None
                            )
                            assert frame.event != "stream_error", frame.data
                            saw_prefill |= frame.event == "stream_prefill"
                            watching.feed(frame)
                            if frame.event == "stream_checkpoint":
                                break
                            if saw_prefill:
                                assert watching.cursor == before
                        assert saw_prefill == recovery
                    assert watching.agents["agent:alice"].complete(record.id)
                    assert watching.cursor != before
                # A committed generation replacement remains recoverable after its
                # control is trimmed from the event stream.
                import toolang.teaming.event_backend as event_backend

                monkeypatch.setattr(event_backend, "MAX_STREAM_EVENTS", 1)
                await exporter.recover("source_gap")
                await exporter.recover("source_gap")
                recovered, frames = await snapshot(initial.cursor)
                assert frames[0].event == "stream_prefill"
                assert recovered.agents["agent:alice"].complete(record.id)
                result = await asyncio.to_thread(
                    subprocess.run,
                    [
                        sys.executable,
                        "-m",
                        "toolang.cli.toolang.main",
                        "--root",
                        str(tmp_path),
                        "top",
                        "--once",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env={**os.environ, "TOOLANG_TMUX": "0"},
                )
                assert result.returncode == 0, result.stderr
                assert isinstance(result.stdout, str)
                assert "agent:alice" in result.stdout and "online" in result.stdout

                def terminal():
                    session = ChatTuiPtySession.start(
                        "toolang.cli.toolang.main", "--root", tmp_path, "top"
                    )
                    try:
                        output = session.wait_for("agent:alice", "online", "quit")
                        assert "Traceback" not in output
                        session.send(b"q")
                        assert session.wait_for_exit() == 0, session.output
                    finally:
                        session.close()

                await asyncio.to_thread(terminal)
            finally:
                exporter.close()
                await driver.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend_outage", [False, True])
def test_resident_shutdown_drains_or_bounds_backend_outage(
    valkey, running_hub, tmp_path, backend_outage
):
    home = tmp_path / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text(
        "flow busy(_: Text):\n  repeat 5000 times:\n    let result = Done\n"
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = [
        sys.executable,
        "-m",
        "toolang.cli.toolang.main",
        "--root",
        str(tmp_path),
    ]

    def cli(*args):
        return subprocess.run(
            [*command, *args], capture_output=True, text=True, timeout=40
        )

    async def scenario():
        driver = Backend(valkey)
        service = EventBackend(driver)
        connection = running_hub.connection()
        endpoint = f"http://127.0.0.1:{port}"
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as http:
                await service.initialize()
                async with asyncio.timeout(10):
                    while True:
                        _, origins, _ = await service.capture("agent:alice")
                        if origins.get("agent:alice", {}).get("status") == "complete":
                            break
                        await asyncio.sleep(0.01)
                response = await http.post(
                    f"{endpoint}/api/v1/threads", json={"client": "script"}
                )
                response.raise_for_status()
                root = None
                async with aconnect_sse(
                    http,
                    "POST",
                    f"{endpoint}/api/v1/runs/authored/stream",
                    json={
                        "thread_id": response.json()["thread"]["id"],
                        "request_id": "shutdown",
                        "runnable": {"ref": "flow:busy", "input": {"_": "Done"}},
                        "model": None,
                        "policy": {"allow": [], "limits": {}},
                    },
                ) as stream:
                    stream.response.raise_for_status()
                    async for event in stream.aiter_sse():
                        if event.event == "run_begin":
                            root = json.loads(event.data)["run"]
                        if event.event == "step_begin":
                            break
                assert root is not None
                pid = (
                    (await driver._call("INFO", "server"))["process_id"]
                    if backend_outage
                    else None
                )
                if pid is not None:
                    os.kill(pid, signal.SIGSTOP)
                try:
                    stopped = await asyncio.to_thread(cli, "stop", "alice")
                finally:
                    if pid is not None:
                        os.kill(pid, signal.SIGCONT)
                assert stopped.returncode == 0, stopped.stderr
                if backend_outage:
                    return
                state = HubStreamState(HubScope("agent:alice", run=root))
                async with aconnect_sse(
                    http,
                    "GET",
                    f"{connection.endpoint}/events/stream",
                    headers={"Authorization": f"Bearer {connection.token}"},
                    params={"agent": "agent:alice", "run": root},
                ) as stream:
                    stream.response.raise_for_status()
                    async for event in stream.aiter_sse():
                        if not event.data:
                            continue
                        frame = StreamFrame(
                            event.event, json.loads(event.data), event.id or None
                        )
                        assert frame.event != "stream_error", frame.data
                        state.feed(frame)
                        if frame.event == "run_end" and frame.data["run"] == root:
                            assert frame.data["status"] == "canceled"
                assert state.agents["agent:alice"].complete(root)
                assert state.status["agent:alice"]["complete"]
                assert not state.status["agent:alice"]["online"]
        finally:
            await driver.close()

    try:
        started = cli("start", "alice", "--sandbox", "host", "--port", str(port))
        assert started.returncode == 0, started.stderr
        asyncio.run(scenario())
    finally:
        cli("stop", "alice", "--force")
