"""Read-only activity routing, bounded rows, and explicit snapshot failures."""

import asyncio
from contextlib import closing
import io
import sqlite3
from types import SimpleNamespace

import pytest
import httpx
import typer
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from toolang.cli.common.activity import watch
from toolang.cli.toolang import main as cli
from toolang.cli.toolang.commands import top
from toolang.common.layout import AgentLayout
from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution.store import RunStore
from toolang.teaming.observation import LocalObservation, frames
from tests.unit.execution.test_activity import root


def test_top_observes_hub_without_preparing_an_agent(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        top.HubProcess,
        "connection",
        lambda self: SimpleNamespace(
            endpoint="http://hub", identity="backend", human="human:owner"
        ),
    )

    async def observe(endpoint, **options):
        seen.append((endpoint, options["agent"], options["backend"], options["once"]))

    monkeypatch.setattr(top, "watch", observe)
    assert cli.main(["--root", str(tmp_path), "top", "--once"]) == 0
    assert seen == [("http://hub", None, "backend", True)]
    assert not (tmp_path / "agents").exists()


@pytest.mark.parametrize("kind", ["resident", "roaming", "visiting"])
def test_top_uses_layout_without_requiring_an_existing_service(
    tmp_path, monkeypatch, kind
):
    from toolang.cli.common.context import CliContext

    layout = AgentLayout(tmp_path, "alice", kind)
    layout.home.mkdir(parents=True, exist_ok=True)
    seen = []

    def status(self, **kwargs):
        assert self.layout == layout and not kwargs["check_health"]
        return SimpleNamespace(status="running", endpoint="http://agent")

    monkeypatch.setattr(top.AgentProcess, "status", status)

    async def observe(endpoint, **options):
        assert options["source"].sources[0].reader.path == layout.run_store
        assert options["source"].sources[0].presence() == "online"
        seen.append((endpoint, options["agent"], options["once"]))

    monkeypatch.setattr(top, "watch", observe)
    context = typer.Context(
        typer.main.get_command(cli.app), obj=CliContext(tmp_path, layout=layout)
    )
    top.top_command(context, once=True)
    assert seen == [(None, "agent:alice", True)]


def test_once_does_not_turn_transport_failure_into_an_empty_snapshot(monkeypatch):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, json={"code": "backend_unavailable"})
    )
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=transport)
    )
    output = io.StringIO()
    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(
            watch(
                "http://hub",
                agent=None,
                backend="backend",
                once=True,
                console=Console(file=output),
            )
        )
    assert output.getvalue() == ""


def test_local_details_handles_store_becoming_unsupported(tmp_path, monkeypatch):
    from toolang.cli.common import activity

    monkeypatch.setenv("TERM", "xterm-256color")
    with closing(RunStore(tmp_path / "runs.db")) as store:
        root(store)
    reader = ActivityReader(store.db_path, "agent:alice")
    pages = reader.pages(ActivityQuery())
    with closing(sqlite3.connect(store.db_path)) as connection:
        connection.execute("PRAGMA user_version=48")
    source = LocalObservation(reader)
    output = io.StringIO()

    async def scenario(pipe):
        ready = asyncio.Event()

        async def updates(query):
            # Details may still refer to a row from the previous snapshot.
            for frame in frames(pages):
                yield frame
            ready.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(source, "updates", updates)
        task = asyncio.create_task(
            watch(
                None,
                agent=reader.agent,
                backend=None,
                once=False,
                console=Console(file=output, force_terminal=True, width=100, height=30),
                view="execution",
                source=source,
                refresh=0.01,
            )
        )
        try:
            async with asyncio.timeout(3):
                await ready.wait()
                pipe.send_text("\r")
                while "Unavailable" not in output.getvalue():
                    await asyncio.sleep(0.01)
                pipe.send_text("q")
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    with create_pipe_input() as pipe:
        monkeypatch.setattr(activity, "create_input", lambda **kwargs: pipe)
        monkeypatch.setattr(activity, "create_output", lambda **kwargs: DummyOutput())
        asyncio.run(scenario(pipe))
