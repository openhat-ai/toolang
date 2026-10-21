import asyncio
from dataclasses import replace

import pytest

from toolang.base.types.model import ModelOverride
from toolang.common.layout import AgentLayout
from toolang.up.contract import require_compatible, startup_contract
from toolang.up.instance import update_instance
from toolang.up.sandbox import resolve_launch


def test_explicit_settings_match_without_repeating_unrelated_bindings(tmp_path):
    layout = AgentLayout.resident(tmp_path, "aid")
    spec = asyncio.run(
        resolve_launch(
            layout=layout,
            sandbox="host",
            port=8123,
            environ={"PROVIDER_API_KEY": "secret-key"},
            drivers=frozenset({"chat"}),
            default_overrides={"model": ModelOverride(identity="test/private-model")},
            limit_overrides={"tokens": 1000, "time": 60},
            workspace_additions={"repo": "/one", "other": "/two"},
        )
    )
    saved = startup_contract(spec)
    assert "secret-key" not in str(saved)
    assert "private-model" not in str(saved)
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(startup_contract=saved),
    )
    require_compatible(layout, {})
    require_compatible(layout, {"port": 8123})
    require_compatible(layout, {"limit_overrides": {"tokens": 1000}})
    require_compatible(layout, {"workspace_additions": {"repo": "/one"}})
    with pytest.raises(ValueError, match="startup parameters differ: port.*restart"):
        require_compatible(layout, {"port": 8124})
    with pytest.raises(ValueError, match="workspace_additions"):
        require_compatible(layout, {"workspace_additions": {"repo": "/elsewhere"}})
    with pytest.raises(ValueError, match="limit_overrides"):
        require_compatible(layout, {"limit_overrides": {"tokens": 2000}})


def test_only_explicit_requirements_need_saved_startup_bindings(tmp_path):
    layout = AgentLayout.resident(tmp_path, "aid")
    require_compatible(layout, {})
    with pytest.raises(ValueError, match="unverifiable"):
        require_compatible(layout, {"port": 8123})


def test_environment_and_launch_bookkeeping_do_not_change_settings(tmp_path):
    layout = AgentLayout.resident(tmp_path, "aid")
    spec = asyncio.run(
        resolve_launch(
            layout=layout,
            sandbox="host",
            port=8123,
            environ={"PROVIDER_API_KEY": "original", "PWD": "/one"},
        )
    )
    assert startup_contract(spec) == startup_contract(
        replace(
            spec,
            serve=replace(spec.serve, lifetime="foreground", launch_id="replacement"),
            environ={"PROVIDER_API_KEY": "changed", "PWD": "/two", "TERM": "other"},
            dotenv_envs={"OTHER": "changed"},
        )
    )


def test_legacy_aggregate_bindings_still_verify_exact_explicit_settings(tmp_path):
    layout = AgentLayout.resident(tmp_path, "aid")
    spec = asyncio.run(
        resolve_launch(
            layout=layout,
            sandbox="host",
            port=8123,
            environ={},
            workspace_additions={"repo": "/one", "other": "/two"},
        )
    )
    saved = {
        key: value for key, value in startup_contract(spec).items() if "." not in key
    }
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(startup_contract=saved),
    )
    require_compatible(layout, {})
    require_compatible(
        layout, {"workspace_additions": {"repo": "/one", "other": "/two"}}
    )
    with pytest.raises(ValueError, match="workspace_additions.*restart"):
        require_compatible(layout, {"workspace_additions": {"repo": "/one"}})
