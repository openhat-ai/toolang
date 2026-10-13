from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from toolang.base.types.model import ModelOverride
from toolang.cli.common.policy import (
    resolve_default_overrides,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.common.layout import AgentLayout
from toolang.up import server
from toolang.up.server import build_serve_argv, resolve_serve


def test_server_initializes_setup_and_state_once_concurrently() -> None:
    entered: set[str] = set()
    release = asyncio.Event()

    class Watcher:
        def __init__(self, name: str) -> None:
            self.name = name
            self.calls = 0

        async def refresh(self) -> None:
            self.calls += 1
            entered.add(self.name)
            if entered == {"setup", "state"}:
                release.set()
            await release.wait()

    setup = Watcher("setup")
    state = Watcher("state")

    asyncio.run(
        server._refresh_core(SimpleNamespace(setup=setup, state=state))  # type: ignore[arg-type]
    )

    assert setup.calls == 1
    assert state.calls == 1


def test_resident_default_port_reuses_free_record_or_selects_available(
    tmp_path, monkeypatch
):
    layout = AgentLayout.resident(tmp_path, "alice")
    monkeypatch.setattr(server.agents, "preferred_runtime_port", lambda layout: 7123)
    monkeypatch.setattr(
        server.agents, "assigned_runtime_ports", lambda *args, **kwargs: {7001}
    )
    monkeypatch.setattr(server, "_port_is_available", lambda host, port: port != 7002)
    assert (
        server.resolve_runtime_port(
            host="127.0.0.1", explicit_port=None, layout=layout, temporary=False
        )
        == 7123
    )
    monkeypatch.setattr(
        server, "_port_is_available", lambda host, port: port not in {7123, 7002}
    )
    assert (
        server.resolve_runtime_port(
            host="127.0.0.1", explicit_port=None, layout=layout, temporary=False
        )
        == 7003
    )
    assert (
        server.resolve_runtime_port(
            host="127.0.0.1", explicit_port=7223, layout=layout, temporary=False
        )
        == 7223
    )


@pytest.mark.parametrize(
    "preferred,expected", [(None, 7002), (7001, 7002), (7123, 7123), (8123, 8123)]
)
def test_default_agent_port_reserves_hub_port_without_changing_other_records(
    tmp_path, monkeypatch, preferred, expected
):
    layout = AgentLayout.resident(tmp_path, "alice")
    monkeypatch.setattr(server.agents, "preferred_runtime_port", lambda _: preferred)
    monkeypatch.setattr(server.agents, "assigned_runtime_ports", lambda *a, **kw: set())
    monkeypatch.setattr(server, "_port_is_available", lambda host, port: True)
    assert (
        server.resolve_runtime_port(
            host="127.0.0.1", explicit_port=None, layout=layout, temporary=False
        )
        == expected
    )
    assert (
        server.resolve_runtime_port(
            host="127.0.0.1", explicit_port=7001, layout=layout, temporary=False
        )
        == 7001
    )


def test_auto_agent_ports_skip_assigned_and_busy_ports_and_stop_at_7999(
    tmp_path, monkeypatch
):
    layout = AgentLayout.resident(tmp_path, "alice")
    monkeypatch.setattr(server.agents, "preferred_runtime_port", lambda _: 7001)
    monkeypatch.setattr(
        server.agents, "assigned_runtime_ports", lambda *a, **kw: {7002}
    )
    attempted = []

    def available(host, port):
        attempted.append(port)
        return port == 7999

    monkeypatch.setattr(server, "_port_is_available", available)
    assert (
        server.resolve_runtime_port(
            host="127.0.0.1", explicit_port=None, layout=layout, temporary=False
        )
        == 7999
    )
    assert attempted == list(range(7003, 8000))
    attempted.clear()
    monkeypatch.setattr(server, "_port_is_available", lambda host, port: False)
    with pytest.raises(ValueError, match="no available agent runtime ports"):
        server.resolve_runtime_port(
            host="127.0.0.1", explicit_port=None, layout=layout, temporary=False
        )


def test_temporary_agent_port_selection_is_unchanged(tmp_path, monkeypatch):
    layout = AgentLayout.resident(tmp_path, "alice")
    monkeypatch.setattr(server, "_pick_temporary_runtime_port", lambda host: 45678)
    assert (
        server.resolve_runtime_port(
            host="127.0.0.1", explicit_port=None, layout=layout, temporary=True
        )
        == 45678
    )


def test_serve_argv_contains_only_server_inputs(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    spec = resolve_serve(
        layout=layout,
        host="127.0.0.1",
        endpoint_host="localhost",
        port=8123,
        ceiling_overrides={
            "models": ("openai/gpt-5",),
            "tools": (),
            "skills": None,
        },
        default_overrides={
            "model": ModelOverride(identity="openai/gpt-5"),
            "runnable": None,
        },
        compact_override=ModelOverride(identity="test/compact", effort="low"),
        limit_overrides={
            "agic_model_calls": 25,
            "tokens": 1000,
            "cost": 1.5,
            "time": None,
        },
        log_spec="toolang.up=debug",
    )

    argv = build_serve_argv(
        spec,
        root=Path("/root/.toolang"),
        host="0.0.0.0",
    )

    assert spec.ceiling_overrides == {
        "models": ("openai/gpt-5",),
        "tools": (),
        "skills": None,
    }
    assert argv[:9] == (
        "--root",
        "/root/.toolang",
        "_serve",
        "alice",
        "--host",
        "0.0.0.0",
        "--endpoint-host",
        "localhost",
        "--port",
    )
    assert "--sandbox" not in argv
    assert "--sandbox-child" not in argv
    assert _option_values(argv, "--compact-model") == ["test/compact effort=low"]
    assert _option_values(argv, "--allow") == [
        "models=openai/gpt-5",
        "tools=none",
        "skills=all",
    ]
    assert _option_values(argv, "--default") == [
        "model=openai/gpt-5",
        "runnable=none",
    ]
    assert _option_values(argv, "--limit") == [
        "agic_model_calls=25",
        "tokens=1000",
        "cost=1.5",
        "time=none",
    ]
    assert resolve_ceiling_overrides({}, _option_values(argv, "--allow")) == dict(
        spec.ceiling_overrides
    )
    assert resolve_default_overrides({}, _option_values(argv, "--default")) == dict(
        spec.default_overrides
    )
    assert resolve_limit_overrides({}, _option_values(argv, "--limit")) == dict(
        spec.limit_overrides
    )
    assert argv[-2:] == ("--log", "toolang.up=debug")


def _option_values(argv: tuple[str, ...], name: str) -> list[str]:
    return [argv[index + 1] for index, item in enumerate(argv) if item == name]
