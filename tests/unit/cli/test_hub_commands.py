"""Root Hub routing and concrete port resolution for both lifecycle commands."""

from types import SimpleNamespace

import pytest

from toolang.cli.toolang import main as cli
from toolang.cli.toolang.commands import hub
from toolang.cli.common.ports import agent_port
from toolang.common.layout import AgentLayout
from toolang.up.config import resolve_port_override


@pytest.mark.parametrize("command", ["start", "serve"])
@pytest.mark.parametrize(
    "option,env,configured,expected",
    [
        (None, None, None, 7000),
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


def test_root_api_settings_rejected_and_talk_requires_running_hub(tmp_path, capsys):
    layout = AgentLayout.resident(tmp_path, "alice")
    (tmp_path / "config.toml").write_text("[api]\nport = 7001\n")
    with pytest.raises(ValueError, match="agent home scope"):
        agent_port(layout, None, environ={})
    assert cli.main(["--root", str(tmp_path), "talk"]) != 0
    assert "too hub start" in capsys.readouterr().err
    assert cli.main(["--root", str(tmp_path), "hub", "status"]) == 0
    assert "Hub stopped" in capsys.readouterr().out
