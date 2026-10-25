"""Bare Hub status output and concrete port resolution for lifecycle commands."""

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


@pytest.mark.parametrize("arguments", [[], ["--help"]])
def test_hub_help_never_inspects_runtime_or_config(
    tmp_path, monkeypatch, capsys, arguments
):
    monkeypatch.setattr("sys.argv", ["too"])
    (tmp_path / "config.toml").write_text("bad {{{")
    monkeypatch.setattr(
        hub.HubProcess, "current", lambda _: pytest.fail("help inspected process")
    )
    monkeypatch.setattr(
        hub.httpx, "Client", lambda **_: pytest.fail("help opened network")
    )
    assert cli.main(["--root", str(tmp_path), "hub", *arguments]) == 0
    text = capsys.readouterr().out
    assert text.startswith("Manage the hub service.\n\nUsage: too hub [COMMAND]\n")
    assert text.index("Usage:") < text.index("Commands:")
    commands = text.split("Commands:\n", 1)[1].split("\n\n", 1)[0]
    assert [line.split()[0] for line in commands.splitlines()] == [
        "status",
        "start",
        "stop",
        "serve",
    ]
    assert "Show hub status" in commands


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0", "::"])
@pytest.mark.parametrize("metadata", ["valid", "offline", "invalid"])
def test_hub_status_uses_record_and_keeps_up_when_version_unavailable(
    tmp_path, monkeypatch, capsys, host, metadata
):
    process = hub.HubProcess(tmp_path)
    process.path.parent.mkdir()
    record = HubRecord(
        pid=os.getpid(), created=psutil.Process().create_time(), host=host, port=9999
    )
    record.save(process.path)
    (tmp_path / "config.toml").write_text("[teaming]\nhub = 42\n[hub]\nport = 7777\n")
    client_type = httpx.Client
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url) == record.endpoint + "/info"
        if metadata == "offline":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(
            200,
            json={}
            if metadata == "invalid"
            else {
                "version": "server",
                "api_version": 0,
                "identity": "dataset",
            },
        )

    monkeypatch.setattr(
        hub.httpx,
        "Client",
        lambda **kw: client_type(**kw, transport=httpx.MockTransport(respond)),
    )
    assert cli.main(["--root", str(tmp_path), "hub", "status"]) == 0
    version = "server" if metadata == "valid" else "unknown"
    endpoint = f"[{host}]:9999" if ":" in host else f"{host}:9999"
    assert (
        capsys.readouterr().out == f"hub running: {version}, {endpoint}, {record.pid}\n"
    )
    assert len(requests) == 1


def test_hub_status_ignores_malformed_root_configuration(tmp_path, monkeypatch, capsys):
    process = hub.HubProcess(tmp_path)
    process.path.parent.mkdir()
    record = HubRecord(
        pid=os.getpid(), created=psutil.Process().create_time(), port=9999
    )
    record.save(process.path)
    (tmp_path / "config.toml").write_text("bad {{{")
    client_type = httpx.Client
    monkeypatch.setattr(
        hub.httpx,
        "Client",
        lambda **kw: client_type(
            **kw,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "version": "server",
                        "api_version": 0,
                        "identity": "dataset",
                    },
                )
            ),
        ),
    )
    assert cli.main(["--root", str(tmp_path), "hub", "status"]) == 0
    assert capsys.readouterr().out == (
        f"hub running: server, 127.0.0.1:9999, {record.pid}\n"
    )


@pytest.mark.parametrize(
    "local,expected",
    [
        ("absent", "hub not running"),
        ("starting", "hub not running"),
        ("stale", "hub not running"),
        ("corrupt", "hub unknown:"),
        ("denied", "hub unknown:"),
    ],
)
def test_hub_status_distinguishes_down_and_unverifiable(
    tmp_path, monkeypatch, capsys, local, expected
):
    process = hub.HubProcess(tmp_path)
    if local == "denied":

        def denied(_):
            raise psutil.AccessDenied(os.getpid())

        monkeypatch.setattr(hub.HubProcess, "current", denied)
    elif local != "absent":
        process.path.parent.mkdir()
        if local == "corrupt":
            process.path.write_text("invalid")
        else:
            HubRecord(
                pid=os.getpid(),
                created=psutil.Process().create_time()
                - (10 if local == "stale" else 0),
                port=9999,
                status="starting",
            ).save(process.path)
    monkeypatch.setattr(
        hub.httpx, "Client", lambda **_: pytest.fail("down/unknown must not probe")
    )
    assert cli.main(["--root", str(tmp_path), "hub", "status"]) == 0
    text = capsys.readouterr().out
    assert len(text.splitlines()) == 1 and text.startswith(expected)


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
    assert cli.main(["--root", str(tmp_path), "ps"]) == 0
    output = capsys.readouterr().out
    assert output.splitlines() == [
        "0 agents",
        "hub not running",
    ]
