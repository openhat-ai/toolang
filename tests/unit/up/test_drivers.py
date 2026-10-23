from __future__ import annotations

import asyncio
from itertools import combinations
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from pydantic import TypeAdapter
import pytest

from toolang.api.app import create_app
from toolang.catalog import CapsManager, ChoresManager
from toolang.cli.common import agent_server
from toolang.common.layout import AgentLayout
from toolang.up.config import default_drivers, format_drivers, parse_drivers
from toolang.up.core import AgentCore
from toolang.up.server import build_serve_argv, resolve_serve
from toolang.execution.types import DriverName, ThreadKind


@pytest.mark.parametrize("prefix", ["web", "term", "spawn", "worker", "script"])
def test_obsolete_thread_prefixes_cannot_create_contexts(tmp_path, prefix):
    core = AgentCore(AgentLayout.resident(tmp_path, "alice"))
    try:
        with pytest.raises(ValueError):
            core.threads.create(kind=prefix)
        assert not core.store.list_threads()
        assert {item.value for item in ThreadKind} == {
            "chat",
            "talk",
            "chore",
            "work",
            "exec",
        }
    finally:
        asyncio.run(core.close())


SETS = [
    frozenset(items)
    for size in range(4)
    for items in combinations(("chat", "talk", "chore"), size)
]


@pytest.mark.parametrize("drivers", SETS)
def test_driver_selection_roundtrips_host_guest_and_preserves_management(
    tmp_path, drivers
):
    layout = AgentLayout.resident(tmp_path, "alice")
    spec = resolve_serve(layout=layout, port=8123, drivers=drivers)
    argv = build_serve_argv(spec, place=tmp_path / "guest")
    assert parse_drivers(argv[argv.index("--enable") + 1]) == drivers
    core = AgentCore(layout, drivers=drivers)
    app = create_app(
        core, CapsManager(layout), ChoresManager(layout), cors_allowed_origins=()
    )
    try:
        with TestClient(app) as client:
            assert client.get("/healthz").json() == {"ok": True}
            assert client.get("/api/v0/threads").status_code == 200
            response = client.post("/api/v0/threads", json={})
            assert response.status_code == (201 if "chat" in drivers else 409)
            if "chat" in drivers:
                assert response.json()["thread"]["id"].startswith("chat_")
            else:
                for path in [
                    "authored/stream",
                    "run_absent/rerun/stream",
                    "run_absent/steer",
                ]:
                    response = client.post(f"/api/v0/chat/runs/{path}", json={})
                    assert response.status_code == 409
                    assert response.json()["detail"]["driver"] == "chat"
                assert core.store.list_threads() == []
                assert core.store.list_runs() == []
            script = client.post(
                "/api/v0/runs/threads",
                json={"client_id": "test-script", "request_id": "script-context"},
            )
            assert script.status_code == 201
            assert script.json()["thread"]["id"].startswith("exec_")
            assert "origin" not in script.json()["thread"]
            assert "kind" not in script.json()["thread"]

    finally:
        asyncio.run(core.close())


@pytest.mark.parametrize(
    "value", ["", "none,chat", "chat,unknown", "chat,", "jobs", "messaging"]
)
def test_invalid_driver_selection(value):
    with pytest.raises(ValueError, match="--enable expects chat,chore,talk or none"):
        parse_drivers(value)


@pytest.mark.parametrize(
    ("drivers", "expected"),
    [
        ((), "none"),
        (("chat",), "chat"),
        (("chore",), "chore"),
        (("talk",), "talk"),
        (("chore", "chat"), "chat,chore"),
        (("talk", "chat"), "chat,talk"),
        (("talk", "chore"), "chore,talk"),
        (("talk", "chore", "chat"), "chat,chore,talk"),
    ],
)
def test_driver_display_and_serve_arguments_follow_complexity_order(
    tmp_path, drivers, expected
):
    selection = frozenset(drivers)
    assert format_drivers(selection) == expected
    spec = resolve_serve(
        layout=AgentLayout.resident(tmp_path, "alice"), drivers=selection
    )
    argv = build_serve_argv(spec)
    assert argv[argv.index("--enable") + 1] == expected


def test_driver_schema_lists_names_in_complexity_order():
    assert TypeAdapter(DriverName).json_schema()["enum"] == ["chat", "chore", "talk"]
    assert TypeAdapter(ThreadKind).json_schema()["enum"] == [
        "chat",
        "chore",
        "talk",
        "work",
        "exec",
    ]


def test_placement_defaults():
    assert default_drivers("resident") == {"chat", "talk", "chore"}
    assert default_drivers("visiting") == {"chat", "talk"}
    assert default_drivers("roaming") == set()
    assert parse_drivers("talk,chat,talk") == {"chat", "talk"}
    assert format_drivers(frozenset()) == "none"


@pytest.mark.parametrize(
    "requested,running,error",
    [
        (None, ["chat"], None),
        (None, ["chat", "talk", "chore"], None),
        (frozenset(("chat",)), ["chat"], None),
        (frozenset(("chat", "talk")), ["talk", "chat"], None),
        (frozenset(("chat",)), ["chat", "talk"], "differs"),
        (None, ["talk"], "disabled"),
        (frozenset(("talk",)), ["talk"], "disabled"),
    ],
)
def test_chat_reuse_preserves_running_selection(
    tmp_path, monkeypatch, requested, running, error
):
    from toolang.cli.common.progress import make_cli_progress

    layout = AgentLayout.resident(tmp_path, "alice")
    state = {"drivers": running}
    status = SimpleNamespace(
        status="running", sandbox="host", endpoint="http://127.0.0.1:7001", port=7001
    )
    from toolang.up.contract import startup_contract
    from toolang.up.identity import identity_bindings
    from toolang.up.instance import update_instance

    spec = agent_server._resolve_launch(
        layout,
        sandbox="host",
        dev=None,
        model_catalog=None,
        base_environ={},
        drivers=frozenset(running),
        active_port=7001,
    )
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(
            **identity_bindings(layout),
            startup_contract=startup_contract(spec),
            drivers=running,
        ),
    )
    monkeypatch.setattr(
        agent_server.agents,
        "AgentProcess",
        lambda _: SimpleNamespace(status=lambda **_: status, state=lambda: state),
    )
    args: dict[str, Any] = dict(
        sandbox=None,
        dev=None,
        model_catalog=None,
        ui_base_url="",
        base_environ={},
        progress=make_cli_progress(enabled=False),
        compact_override=None,
        workspace_additions=None,
        temporary=False,
        drivers=requested,
        required_driver="chat",
    )
    if error:
        with pytest.raises(agent_server.AgentServerAcquisitionError, match=error):
            agent_server._prepare_agent_server(layout, **args)
    else:
        result = agent_server._prepare_agent_server(layout, **args)
        assert isinstance(result, agent_server.AgentServerRef)
        assert result.endpoint == status.endpoint
    assert state == {"drivers": running}


def test_partial_startup_closes_execution_store(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from toolang.up import server

    core = MagicMock()
    core.close = AsyncMock()
    monkeypatch.setattr(server, "AgentCore", lambda *a, **k: core)
    monkeypatch.setattr(
        server, "_refresh_core", AsyncMock(side_effect=ValueError("invalid state"))
    )
    monkeypatch.setattr(server, "_restore_termination_signal_defaults", lambda: None)
    monkeypatch.setattr(server, "configure_logging", lambda **kwargs: None)
    with pytest.raises(ValueError, match="invalid state"):
        server.serve(
            resolve_serve(layout=AgentLayout.resident(tmp_path, "alice")),
            environ={},
            sandbox="test",
        )
    core.close.assert_awaited_once()


def test_cli_enable_option_is_command_specific(tmp_path, capsys):
    from toolang.cli.toolang.main import main
    from toolang.catalog.agent import LocalAgents

    LocalAgents(tmp_path / "agents").create("alice", content="agic:\n  {{_}}\n")
    for args in (["serve", "--help"], ["start", "--help"], ["alice", "chat", "--help"]):
        assert main(["--root", str(tmp_path), *args]) == 0
        output = capsys.readouterr().out
        assert "--enable" in output
        assert "chat,chore,talk" in output
        assert "--driver" not in output
    assert main(["--help"]) == 0
    assert "--enable" not in capsys.readouterr().out
    assert main(["talk", "--enable", "chat"]) != 0
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "alice", "start", "--driver", "chat"]) != 0
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "alice", "chore", "--help"]) == 0
    output = capsys.readouterr().out
    for command in ("list", "new", "edit", "delete", "run", "cancel"):
        assert command in output
    for command in ("archive", "draft", "ready", "reopen"):
        assert command not in output
