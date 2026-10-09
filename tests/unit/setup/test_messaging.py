"""Root and home teaming configuration must remain separate."""

from pathlib import Path

import pytest

from toolang.common.config_sources import ConfigSource
from toolang.teaming.config import BackendConfig
from toolang.teaming.errors import TeamingError
from toolang.setup.teaming import resolve_teaming_setup

ROOT = Path("/root/config.toml")
HOME = Path("/root/agents/alice/config.toml")


def resolve(root=None, home=None, extra=()):
    return resolve_teaming_setup(
        (ConfigSource(ROOT, root or {}), ConfigSource(HOME, home or {}), *extra),
        root=ROOT,
        home=HOME,
        default_human="owner",
    )


@pytest.mark.parametrize("home", [{}, {"teaming": {}}, {"teaming": {"enabled": True}}])
def test_defaults_enable_agent_teaming(home):
    setup = resolve(home=home)
    assert setup.root.backend == BackendConfig("redis://localhost:6379/0")
    assert setup.root.human == "human:owner"
    assert setup.root.hub_port == 7000
    assert setup.home.enabled
    assert setup.toolset_config() == {"url": setup.root.backend.url}


def test_scopes_resolve_without_merging():
    root = {
        "teaming": {
            "human": "brice",
            "backend": {"url": "redis://shared"},
            "hub": {"port": 8000},
        }
    }
    enabled = resolve(root, {"teaming": {"enabled": True}})
    assert enabled.root.human == "human:brice" and enabled.root.hub_port == 8000
    assert enabled.toolset_config() == {"url": "redis://shared"}
    assert resolve(root, {"teaming": {"enabled": False}}).toolset_config() == {}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "bad",
        {"enabled": True},
        {"groups": []},
        {"backend": {"url": "bad"}},
        {"backend": {"url": None}},
        {"backend": {"groups": []}},
        {"human": "a:b"},
        {"hub": {"port": True}},
        {"hub": {"port": 0}},
        {"hub": {"unknown": 1}},
    ],
)
def test_invalid_root_reports_source(raw):
    with pytest.raises(TeamingError, match=str(ROOT)):
        resolve({"teaming": raw})


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "bad",
        {"enabled": "false"},
        {"human": "other"},
        {"backend": {"url": "redis://other"}},
        {"hub": {}},
        {"groups": []},
    ],
)
def test_invalid_home_reports_source(raw):
    with pytest.raises(TeamingError, match=str(HOME)):
        resolve(home={"teaming": raw})


def test_project_projection_and_legacy_configuration_are_rejected():
    with pytest.raises(TeamingError, match="root/home scope"):
        resolve(extra=(ConfigSource(Path("/project/config.toml"), {"teaming": {}}),))
    for section in ("messaging", "human"):
        with pytest.raises(TeamingError, match="replaced by teaming"):
            resolve({section: {}})


@pytest.mark.parametrize(
    "url",
    [
        "redis://host/not-a-database",
        "redis://host/-1",
        "redis://host?db=-1",
        "redis://host?db=bad",
        "redis://host#fragment",
        "redis://host:0",
    ],
)
def test_invalid_backend_url_does_not_silently_select_database_zero(url):
    with pytest.raises(TeamingError, match=str(ROOT)):
        resolve({"teaming": {"backend": {"url": url}}})
