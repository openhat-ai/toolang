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
from toolang.up.records import HubRecord


@pytest.mark.parametrize("healthy", [True, False])
def test_hub_status_queries_configured_url_independently_of_local_process(
    tmp_path, monkeypatch, capsys, healthy
):
    (tmp_path / "config.toml").write_text(
        '[teaming]\nhub = "http://hub.example:7123"\n'
    )
    client_type = httpx.Client
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url).startswith("http://hub.example:7123/")
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={"version": "0.4.0-test", "api_version": 0, "identity": "dataset"},
            )
        assert request.headers["X-Toolang-Backend"] == "dataset"
        return httpx.Response(200 if healthy else 503, json={"ok": healthy})

    monkeypatch.setattr(
        hub.httpx,
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
    assert output.splitlines()[0].split() == [
        "hub",
        "running" if healthy else "unavailable",
        "v0.4.0-test",
    ]
    assert "alice" in output and "1 agents, 1 running, 1 talk enabled" in output
    assert "Hub URL: http://hub.example:7123" in output
    assert "Local Hub: not running" in output
    assert [r.url.path for r in requests] == ["/info", "/healthz"]


@pytest.mark.parametrize("local", ["starting", "stale", "corrupt", "denied"])
def test_local_record_does_not_block_target_inspection(
    tmp_path, monkeypatch, capsys, local
):
    process = hub.HubProcess(tmp_path)
    if local == "denied":

        def denied(_):
            raise psutil.AccessDenied(os.getpid())

        monkeypatch.setattr(hub.HubProcess, "current", denied)
    elif local == "corrupt":
        process.path.parent.mkdir()
        process.path.write_text("invalid")
    else:
        HubRecord(
            pid=os.getpid(),
            created=psutil.Process().create_time() - (10 if local == "stale" else 0),
            port=9999,
            status="starting",
        ).save(process.path)
    client_type = httpx.Client

    def respond(request):
        assert request.url.port == 7001
        return httpx.Response(
            200,
            json={"version": "test", "api_version": 0, "identity": "dataset"}
            if request.url.path == "/info"
            else {"ok": True},
        )

    monkeypatch.setattr(
        hub.httpx,
        "Client",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)),
    )
    assert cli.main(["--root", str(tmp_path), "hub"]) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0].split() == ["hub", "running", "vtest"]
    assert (
        "Local Hub: "
        + {
            "starting": "starting",
            "stale": "not running",
            "corrupt": "unknown",
            "denied": "unknown",
        }[local]
        in output
    )
    if local in {"corrupt", "denied"}:
        assert "record inspection failed" in output


@pytest.mark.parametrize("failure", ["offline", "metadata"])
def test_failed_target_inspection_keeps_local_agents_visible(
    tmp_path, monkeypatch, capsys, failure
):
    (tmp_path / "agents" / "alice").mkdir(parents=True)
    client_type = httpx.Client

    def respond(request):
        if failure == "offline":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(404)

    monkeypatch.setattr(
        hub.httpx,
        "Client",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)),
    )
    assert cli.main(["--root", str(tmp_path), "hub"]) == 0
    output = capsys.readouterr().out
    assert "unavailable" in output.splitlines()[0]
    assert "alice" in output and "1 agents" in output


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
    content = "" if configured is None else f"[hub]\nport = {configured}\n"
    (tmp_path / "config.toml").write_text(content)
    seen = []

    def start(self, argv):
        seen.append(int(argv[-1]))
        assert argv[-4:-1] == ["hub", "serve", "--port"]
        return SimpleNamespace(endpoint="http://hub")

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
    tmp_path, capsys, monkeypatch
):
    client_type, async_client_type = httpx.Client, httpx.AsyncClient

    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    def http_client(client, **kwargs):
        kwargs["transport"] = httpx.MockTransport(offline)
        return client(**kwargs)

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: http_client(client_type, **kwargs)
    )
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: http_client(async_client_type, **kwargs)
    )
    layout = AgentLayout.resident(tmp_path, "alice")
    (tmp_path / "config.toml").write_text("[api]\nport = 7001\n")
    with pytest.raises(ValueError, match="agent home scope"):
        agent_port(layout, None, environ={})
    assert cli.main(["--root", str(tmp_path), "convo", "list"]) != 0
    assert "Hub is unavailable at http://127.0.0.1:7001" in capsys.readouterr().err
    assert cli.main(["--root", str(tmp_path), "hub"]) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0].split() == ["hub", "unavailable"]
    assert "no agents" in output
    assert "0 agents, 0 running, 0 talk enabled" not in output
