"""Read-only activity routing, bounded rows, and explicit snapshot failures."""

import asyncio
import io
from types import SimpleNamespace

import pytest
import httpx
import typer
from rich.console import Console

from toolang.cli.common.activity import watch
from toolang.cli.toolang import main as cli
from toolang.cli.toolang.commands import top
from toolang.common.layout import AgentLayout


def test_top_observes_hub_without_preparing_an_agent(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        top.HubProcess,
        "connection",
        lambda self: SimpleNamespace(endpoint="http://hub", identity="backend"),
    )

    async def observe(endpoint, **options):
        seen.append((endpoint, options["agent"], options["backend"], options["once"]))

    monkeypatch.setattr(top, "watch", observe)
    assert cli.main(["--root", str(tmp_path), "top", "--once"]) == 0
    assert seen == [("http://hub", None, "backend", True)]
    assert not (tmp_path / "agents").exists()


@pytest.mark.parametrize("kind", ["resident", "roaming", "visiting"])
def test_top_uses_layout_only_and_requires_an_existing_service(
    tmp_path, monkeypatch, kind
):
    from toolang.cli.common.context import CliContext

    layout = AgentLayout(tmp_path, "alice", kind)
    layout.home.mkdir(parents=True, exist_ok=True)
    seen = []

    def status(self, **kwargs):
        assert self.layout == layout and kwargs["check_health"]
        return SimpleNamespace(status="running", endpoint="http://agent")

    monkeypatch.setattr(top.AgentProcess, "status", status)

    async def observe(endpoint, **options):
        seen.append((endpoint, options["agent"], options["once"]))

    monkeypatch.setattr(top, "watch", observe)
    context = typer.Context(
        typer.main.get_command(cli.app), obj=CliContext(tmp_path, layout=layout)
    )
    top.top_command(context, once=True)
    assert seen == [("http://agent", "agent:alice", True)]


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
