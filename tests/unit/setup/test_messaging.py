"""Client/server settings resolve independently at their own boundaries."""

from pathlib import Path
import pwd
from types import SimpleNamespace

import pytest

from toolang.common.config_sources import ConfigSource
from toolang.setup.teaming import load_teaming_root, resolve_teaming_setup
from toolang.teaming.errors import TeamingError
from toolang.up.config import hub_config

from tests.support.layout import root_file

ROOT = Path("/root/config/config.toml")
HOME = Path("/root/agents/alice/config.toml")


@pytest.mark.parametrize("boundary", ["human", "agent"])
@pytest.mark.parametrize("configured_human", [None, "configured"])
def test_default_human_uses_os_account_despite_inherited_login_name(
    tmp_path, monkeypatch, boundary, configured_human
):
    monkeypatch.setenv("LOGNAME", "bryan")
    monkeypatch.setenv("USER", "brice")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="brice"))
    raw: dict[str, object] = (
        {"teaming": {"human": configured_human}} if configured_human else {}
    )
    root = root_file(tmp_path, "config.toml")
    if configured_human:
        root.write_text(f'[teaming]\nhuman = "{configured_human}"\n')

    config = (
        load_teaming_root(tmp_path)
        if boundary == "human"
        else resolve_teaming_setup(
            (ConfigSource(root, raw),),
            root=root,
            home=tmp_path / "agents" / "alice" / "config.toml",
            host_root=tmp_path,
        ).root
    )

    assert config.human == f"human:{configured_human or 'brice'}"


def resolve(root=None, home=None, extra=()):
    return resolve_teaming_setup(
        (ConfigSource(ROOT, root or {}), ConfigSource(HOME, home or {}), *extra),
        root=ROOT,
        home=HOME,
        host_root=ROOT.parent.parent,
        default_human="owner",
    )


@pytest.mark.parametrize("home", [{}, {"teaming": {}}, {"teaming": {"enabled": True}}])
def test_defaults_enable_agent_teaming(home):
    setup = resolve(home=home)
    assert setup.root.human == "human:owner"
    assert setup.root.hub == "http://127.0.0.1:7001"
    assert setup.enabled
    assert setup.toolset_config() == {
        "root": "/root",
        "hub": "http://127.0.0.1:7001",
    }


def test_clients_ignore_server_settings_and_preserve_root_identity():
    raw = {
        "teaming": {"human": "brice", "hub": "https://hub.example/team/"},
        "hub": {"backend": "invalid", "port": False},
    }
    setup = resolve(raw)
    assert setup.root.human == "human:brice"
    assert setup.root.hub == "https://hub.example/team"
    assert not resolve(raw, {"teaming": {"enabled": False}}).enabled
    assert not resolve(
        {"teaming": {"enabled": False}}, {"teaming": {"enabled": True}}
    ).enabled
    assert resolve({"teaming": {"enabled": False}}).toolset_config() == {}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "bad",
        {"enabled": "true"},
        {"groups": []},
        {"human": "a:b"},
        {"hub": ""},
        {"hub": "redis://host"},
        {"hub": "http://host:0"},
        {"hub": "http://user:secret@host"},
        {"hub": "http://host?query"},
        {"hub": "http://0.0.0.0:7001"},
    ],
)
def test_invalid_client_settings_report_source(raw):
    with pytest.raises(TeamingError, match=str(ROOT)):
        resolve({"teaming": raw})


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "bad",
        {"enabled": "false"},
        {"human": "other"},
        {"backend": {}},
        {"hub": "http://other"},
    ],
)
def test_home_accepts_only_enablement(raw):
    with pytest.raises(TeamingError, match=str(HOME)):
        resolve(home={"teaming": raw})


def test_unknown_scopes_and_fields_are_invalid():
    for raw in ({"backend": {}}, {"hub": {"port": 8000}}):
        with pytest.raises(TeamingError):
            resolve({"teaming": raw})
    with pytest.raises(TeamingError, match="root/home scope"):
        resolve(extra=(ConfigSource(Path("/project/config.toml"), {"teaming": {}}),))


def test_server_defaults_ignore_client_configuration():
    server = hub_config({"teaming": "invalid"}, source=ROOT, root="/root")
    assert (server.host, server.port) == ("127.0.0.1", 7001)
    assert server.backend.url == "redis://localhost:6379/0"
    assert server.backend.root == "/root"
    assert resolve({"hub": {"port": 8123}}).root.hub.endswith(":7001")


@pytest.mark.parametrize(
    "raw",
    [
        {"port": True},
        {"port": 0},
        {"host": ""},
        {"host": "http://host"},
        {"backend": {"url": None}},
        {"backend": {"url": "redis://host?db=-1"}},
        {"url": "http://host"},
    ],
)
def test_invalid_server_settings_fail_without_client_resolution(raw):
    with pytest.raises((ValueError, TeamingError)):
        hub_config({"hub": raw}, source=ROOT, root="/root")
