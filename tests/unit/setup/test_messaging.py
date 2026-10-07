"""Setup owns messaging defaults, overrides, and human identity."""

import pytest

from toolang.messaging.config import MessagingConfig
from toolang.messaging.errors import MessagingError
from toolang.setup.messaging import resolve_messaging_setup


@pytest.mark.parametrize(
    "raw", [{}, {"messaging": {}}, {"messaging": {"groups": ["gc_dev"]}}]
)
def test_messaging_defaults_to_local_valkey(raw):
    setup = resolve_messaging_setup((raw,), human_config={}, default_human="owner")
    expected_groups = tuple(raw.get("messaging", {}).get("groups", []))
    assert setup.config == MessagingConfig("redis://localhost:6379/0", expected_groups)
    assert setup.human == "owner"


def test_root_and_agent_settings_resolve_once():
    root = {
        "messaging": {"url": "redis://shared", "groups": ["gc_root"]},
        "human": {"name": "bryan"},
    }
    agent = {
        "messaging": {"groups": ["gc_dev", "gc_dev"]},
        "human": {"name": "ignored"},
    }
    setup = resolve_messaging_setup(
        (root, agent), human_config=root, default_human="fallback"
    )
    assert setup.config == MessagingConfig("redis://shared", ("gc_dev",))
    assert setup.human == "bryan"
    assert setup.toolset_config() == {"url": "redis://shared", "groups": ["gc_dev"]}
    disabled = resolve_messaging_setup(
        (root, {"messaging": {"enabled": False}}),
        human_config=root,
        default_human="fallback",
    )
    assert disabled.config is None and disabled.toolset_config() == {}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "bad",
        {"url": "bad"},
        {"url": None},
        {"groups": ["all"]},
        {"groups": "gc_dev"},
        {"enabled": "false"},
    ],
)
def test_explicit_invalid_config_does_not_fall_back(raw):
    with pytest.raises(MessagingError):
        resolve_messaging_setup(
            ({"messaging": raw},), human_config={}, default_human="owner"
        )
