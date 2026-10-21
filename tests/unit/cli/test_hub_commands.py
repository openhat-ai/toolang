"""Bare Hub status output and concrete port resolution for lifecycle commands."""

from toolang.up.workload import ForegroundWorkload
from types import SimpleNamespace
import os

import httpx
import psutil
import pytest

from toolang.cli.toolang import main as cli
from toolang.cli.toolang.commands import hub
from toolang.cli.common.ports import agent_port
from toolang.common.layout import AgentLayout
from toolang.up.config import resolve_port_override
from toolang.teaming.discovery import HubDiscovery


@pytest.mark.parametrize(
    "version,label",
    [
        ("0.4.0a5-3-g12345678*", "v0.4.0a5-3-g12345678*"),
        ("v0.4.0a5-3-g12345678*", "v0.4.0a5-3-g12345678*"),
        ("unknown", "unknown"),
    ],
)
@pytest.mark.parametrize("healthy", [True, False])
def test_hub_status_shows_server_version_in_shared_columns(
    tmp_path, monkeypatch, capsys, version, label, healthy
):
    import toolang.up.hub as module

    saved = HubDiscovery(
        pid=os.getpid(),
        created=psutil.Process().create_time(),
        port=7123,
        human="human:owner",
        identity="backend",
    )
    saved.save(hub.HubProcess(tmp_path).path)
    client_type = httpx.Client
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/healthz":
            return httpx.Response(200 if healthy else 503, json={"ok": healthy})
        assert request.url.path == "/info"
        return httpx.Response(200, json={"version": version, "api_version": 0})

    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)),
    )
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    with ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset({"chat", "talk"})
    ):
        assert cli.main(["--root", str(tmp_path), "hub"]) == 0
        output = capsys.readouterr().out
        rows = output.splitlines()
        assert rows[0].split() == (
            ["hub", "running", label] if healthy else ["hub", "not", "running"]
        )
        assert rows[0].index("running" if healthy else "not") == rows[1].index(
            "running"
        )
        assert "1 agents, 1 running, 1 talk enabled" in output
        assert "alice" in output
        assert "http://127.0.0.1:7123" not in output
        assert requests == ["/healthz", "/info"]


@pytest.mark.parametrize("healthy", [True, False])
def test_hub_status_missing_metadata_keeps_readiness(
    tmp_path, monkeypatch, capsys, healthy
):
    import toolang.up.hub as module

    monkeypatch.setenv("TERM", "xterm-256color")
    saved = HubDiscovery(
        pid=os.getpid(),
        created=psutil.Process().create_time(),
        port=7123,
        human="human:owner",
        identity="backend",
    )
    saved.save(hub.HubProcess(tmp_path).path)
    client_type = httpx.Client

    def respond(request):
        if request.url.path == "/healthz":
            return httpx.Response(200 if healthy else 503, json={"ok": healthy})
        return httpx.Response(404)

    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)),
    )
    assert cli.main(["--root", str(tmp_path), "hub"]) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0].split() == (
        ["hub", "running", "unknown"] if healthy else ["hub", "not", "running"]
    )
    assert output.splitlines()[1:] == ["└── no agents"]
    assert "0 agents, 0 running, 0 talk enabled" not in output


@pytest.mark.parametrize("state", ["starting", "stopped", "stale"])
def test_hub_status_without_ready_process_skips_http(
    tmp_path, monkeypatch, capsys, state
):
    import toolang.up.hub as module

    monkeypatch.setenv("TERM", "xterm-256color")
    if state != "stopped":
        saved = HubDiscovery(
            pid=os.getpid(),
            created=psutil.Process().create_time() - (10 if state == "stale" else 0),
            port=7123,
            human="human:owner",
            identity="backend",
            status="starting" if state == "starting" else "running",
        )
        saved.save(hub.HubProcess(tmp_path).path)

    def fail(**kwargs):
        pytest.fail("Status must not query a stopped, stale, or starting Hub")

    monkeypatch.setattr(module.httpx, "Client", fail)
    assert cli.main(["--root", str(tmp_path), "hub"]) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0].split() == ["hub", "not", "running"]
    assert output.splitlines()[1:] == ["└── no agents"]
    assert "0 agents, 0 running, 0 talk enabled" not in output
    assert "http://127.0.0.1:7123" not in output


def test_hub_inspection_failure_keeps_local_agents_visible(tmp_path, capsys):
    (tmp_path / "agents" / "alice").mkdir(parents=True)
    hub_dir = tmp_path / "hub"
    hub_dir.mkdir()
    (hub_dir / "hub.json").write_text("invalid")

    assert cli.main(["--root", str(tmp_path), "hub"]) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0].split() == ["hub", "not", "running"]
    assert "no agents" in output
    assert "Hub" in output and "alice" not in output
    assert "partial: Hub: record inspection failed" in output


def test_hub_status_command_is_removed(tmp_path, capsys):
    assert cli.main(["--root", str(tmp_path), "hub", "status"]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "No such command 'status'." in captured.err


@pytest.mark.parametrize("command", ["start", "serve"])
@pytest.mark.parametrize(
    "option,env,configured,expected",
    [
        (None, None, None, 7001),
        (None, None, 7200, 7200),
        (None, "7300", 7200, 7300),
        (7400, "invalid", 7200, 7400),
    ],
)
def test_hub_start_and_serve_port_precedence(
    tmp_path, monkeypatch, command, option, env, configured, expected
):
    monkeypatch.delenv("TOOLANG_HUB_PORT", raising=False)
    if env is not None:
        monkeypatch.setenv("TOOLANG_HUB_PORT", env)
    content = "" if configured is None else f"[teaming.hub]\nport = {configured}\n"
    (tmp_path / "config.toml").write_text(content)
    seen = []

    def start(self, argv):
        seen.append(int(argv[-1]))
        assert argv[-4:-1] == ["hub", "serve", "--port"]
        return SimpleNamespace(connection=SimpleNamespace(endpoint="http://hub"))

    def serve(root, config, *, port):
        assert root == tmp_path
        seen.append(port)
        return 0

    monkeypatch.setattr(hub.HubProcess, "start", start)
    monkeypatch.setattr(hub, "serve_hub", serve)
    args = [] if option is None else ["--port", str(option)]
    assert cli.main(["--root", str(tmp_path), "hub", command, *args]) == 0
    assert seen == [expected]
    assert (tmp_path / "config.toml").read_text() == content


@pytest.mark.parametrize("raw", ["", "bad", "0", "65536", "-1", "7.1", "７０００"])
def test_invalid_environment_ports_fail(raw):
    with pytest.raises(ValueError, match="TEST_PORT"):
        resolve_port_override(
            None, environ={"TEST_PORT": raw}, env_name="TEST_PORT", configured=7000
        )


def test_resident_port_precedence_and_temporary_isolation(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.config.write_text("[api]\nport = 7123\n")
    assert agent_port(layout, None, environ={}) == 7123
    assert agent_port(layout, None, environ={"TOOLANG_AGENT_PORT": "7223"}) == 7223
    assert agent_port(layout, 7323, environ={"TOOLANG_AGENT_PORT": "bad"}) == 7323
    visiting = AgentLayout(tmp_path, "temporary", "visiting")
    assert agent_port(visiting, None, environ={"TOOLANG_AGENT_PORT": "bad"}) is None
    layout.config.write_text("")
    assert agent_port(layout, None, environ={}) is None


@pytest.mark.parametrize(
    "config", ["port = true", "port = 0", "port = '7001'", "host = 'localhost'"]
)
def test_invalid_home_api_settings_report_source(tmp_path, config):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.config.write_text("[api]\n" + config + "\n")
    with pytest.raises(ValueError, match="alice/config.toml"):
        agent_port(layout, None, environ={})


def test_root_api_settings_rejected_and_convo_list_requires_running_hub(
    tmp_path, capsys
):
    layout = AgentLayout.resident(tmp_path, "alice")
    (tmp_path / "config.toml").write_text("[api]\nport = 7001\n")
    with pytest.raises(ValueError, match="agent home scope"):
        agent_port(layout, None, environ={})
    assert cli.main(["--root", str(tmp_path), "convo", "list"]) != 0
    assert "too hub start" in capsys.readouterr().err
    assert cli.main(["--root", str(tmp_path), "hub"]) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0].split() == ["hub", "not", "running"]
    assert "no agents" in output
    assert "0 agents, 0 running, 0 talk enabled" not in output
