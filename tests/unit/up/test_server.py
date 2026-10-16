from __future__ import annotations

import asyncio
import os
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


@pytest.mark.parametrize("preferred", [None, 7123])
def test_automatic_port_selection_ignores_invalid_legacy_peers(
    tmp_path, monkeypatch, preferred
):
    from toolang.up.records import update_record

    layout = AgentLayout.resident(tmp_path, "alice")
    if preferred is not None:
        update_record(
            layout.runtime_status,
            lambda entry: entry["control"].update(
                last_successful={"endpoint": f"http://localhost:{preferred}"}
            ),
        )
    for name, port in (
        ("bob", 7002),
        ("old.name", 7123),
        ("old-name", 7123),
        ("Gus", 7123),
        ("a" * 65, 7123),
    ):
        update_record(
            tmp_path / "agents" / name / ".runtime" / "status.json",
            lambda entry, port=port: entry["control"].update(
                last_successful={"endpoint": f"http://localhost:{port}"}
            ),
        )
    monkeypatch.setattr(server, "_port_is_available", lambda host, port: True)
    assert server.resolve_runtime_port(
        host="127.0.0.1", explicit_port=None, layout=layout, temporary=False
    ) == (preferred or 7003)


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
    assert argv[:4] == ("--root", str(tmp_path), "_serve", "alice")
    assert _option_values(argv, "--agent-root") == ["/root/.toolang"]
    assert _option_values(argv, "--host") == ["0.0.0.0"]
    assert _option_values(argv, "--sandbox") == ["host"]
    assert _option_values(argv, "--location") == ["host"]
    assert _option_values(argv, "--source-policy") == ["root"]
    assert "--placement" not in argv
    assert "--sandbox-child" not in argv
    assert _option_values(argv, "--compact-model") == ["test/compact effort=low"]
    assert _option_values(argv, "--allow") == [
        "models=openai/gpt-5",
        "tools=none",
        "skills=all",
    ]
    assert _option_values(argv, "--default") == [
        "model=openai/gpt-5",
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


@pytest.mark.parametrize("location", ["host", "guest"])
@pytest.mark.parametrize("stage", ["construct", "refresh"])
def test_workload_startup_failure_records_error_and_retains_control_reference(
    tmp_path, monkeypatch, location, stage
):
    from unittest.mock import AsyncMock
    from toolang.base.types.sandbox import SandboxRef
    from toolang.up.records import SandboxState, read_record, update_record

    layout = AgentLayout.resident(tmp_path, "alice")
    spec = resolve_serve(layout=layout, port=8123, launch_id="launch")
    update_record(layout.runtime_status, lambda entry: entry.update(launch_id="launch"))
    reference = SandboxState(
        "host" if location == "host" else "docker:image",
        SandboxRef("resource", spec.endpoint, runtime_kind="container"),
        "launch",
    )
    reference.save(layout.runtime_status)
    core = SimpleNamespace(close=AsyncMock())

    def construct(*args, **kwargs):
        if stage == "construct":
            raise ValueError("invalid startup inputs")
        return core

    async def register(*args, **kwargs):
        return reference.ref

    monkeypatch.setattr("toolang.up.sandbox.register_host_server", register)
    monkeypatch.setattr(server, "AgentCore", construct)
    monkeypatch.setattr(
        server,
        "_refresh_core",
        AsyncMock(side_effect=ValueError("invalid startup inputs")),
    )
    monkeypatch.setattr(server, "configure_logging", lambda **kwargs: None)
    with pytest.raises(ValueError, match="invalid startup inputs"):
        server.serve(
            spec,
            environ={},
            sandbox=reference.sandbox,
            launch_id="launch",
            location=location,
        )
    entry = read_record(layout.runtime_status)
    assert entry is not None
    assert entry["workload"]["status"] == "failed"
    assert entry["workload"]["pid"] is None
    assert entry["workload"]["message"] == "invalid startup inputs"
    assert SandboxState.load(layout.runtime_status) == reference
    assert "last_successful" not in entry["control"]
    if stage == "refresh":
        core.close.assert_awaited_once()


@pytest.mark.parametrize("owns_control", [True, False])
def test_cleanup_failure_retains_reference_and_reports_error(
    tmp_path, monkeypatch, owns_control
):
    from unittest.mock import AsyncMock
    from toolang.plugin.sandboxes.host import process_ref
    from toolang.up.records import SandboxState, read_record

    layout = AgentLayout.resident(tmp_path, "alice")
    reference = SandboxState(
        "host",
        process_ref(os.getpid(), "http://localhost:8123", launch_id="launch"),
        "launch",
    )
    reference.save(layout.runtime_status)
    core = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("cleanup failed")))

    async def register(*args, **kwargs):
        return reference.ref

    monkeypatch.setattr("toolang.up.sandbox.register_host_server", register)
    monkeypatch.setattr(server, "AgentCore", lambda *args, **kwargs: core)
    monkeypatch.setattr(
        server, "_refresh_core", AsyncMock(side_effect=ValueError("startup failed"))
    )
    monkeypatch.setattr(server, "configure_logging", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        server.serve(
            resolve_serve(layout=layout, port=8123),
            environ={},
            sandbox="host",
            launch_id=None if owns_control else "launch",
        )
    entry = read_record(layout.runtime_status)
    assert entry is not None and entry["workload"]["status"] == "failed"
    assert "cleanup failed" in entry["workload"]["message"]
    assert SandboxState.load(layout.runtime_status) == reference


def test_guest_without_explicit_launch_identity_does_not_adopt_status(
    tmp_path, monkeypatch
):
    from toolang.up.records import SandboxState
    from toolang.plugin.sandboxes.host import process_ref

    layout = AgentLayout.resident(tmp_path, "alice")
    reference = SandboxState(
        "host", process_ref(os.getpid(), "http://localhost:8123"), "previous"
    )
    reference.save(layout.runtime_status)
    before = layout.runtime_status.read_bytes()
    monkeypatch.setattr(
        server,
        "AgentCore",
        lambda *args, **kwargs: pytest.fail("guest must not execute"),
    )
    with pytest.raises(ValueError, match="launch identity"):
        server.serve(
            resolve_serve(layout=layout, port=8123),
            environ={},
            sandbox="docker:image",
            location="guest",
        )
    assert layout.runtime_status.read_bytes() == before
