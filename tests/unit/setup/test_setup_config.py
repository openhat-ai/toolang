from __future__ import annotations

from pathlib import Path

import pytest

from toolang.base.types.policy import ResourceFilter, RunBindings, RunLimits
from toolang.common.layout import AgentLayout
from toolang.setup.config import (
    load_agent_config,
    load_setup_config,
    load_setup_envs,
    resolve_resource_filter,
    resolve_run_bindings,
    resolve_run_limits,
)


def test_setup_config_reads_only_the_toolang_root(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (root / "config.toml").write_text(
        '[models.providers.gateway]\nendpoint = "https://root.example/v1"\n',
        encoding="utf-8",
    )
    (home / "config.toml").write_text(
        '[models.providers.gateway]\nendpoint = "https://home.example/v1"\n',
        encoding="utf-8",
    )

    config = load_setup_config(AgentLayout.resident(root, "alice"))

    assert config == {
        "models": {"providers": {"gateway": {"endpoint": "https://root.example/v1"}}}
    }

    assert load_agent_config(AgentLayout.resident(root, "alice")) == {
        "models": {"providers": {"gateway": {"endpoint": "https://home.example/v1"}}}
    }


def test_setup_envs_overlay_process_values_on_root_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    root.mkdir()
    (root / ".env").write_text(
        "ROOT_ONLY=from-root\nOVERRIDDEN=from-root\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OVERRIDDEN", "from-process")
    monkeypatch.setenv("PROCESS_ONLY", "from-process")

    envs = load_setup_envs(AgentLayout.resident(root, "alice"))

    assert envs["ROOT_ONLY"] == "from-root"
    assert envs["OVERRIDDEN"] == "from-process"
    assert envs["PROCESS_ONLY"] == "from-process"


def test_setup_policy_overlays_root_agent_and_frozen_overrides() -> None:
    root = {
        "allow": {
            "models": ["gateway/*"],
            "caps": ["service/*"],
            "skills": ["reviewer"],
        },
        "default": {"model": "gateway/chat", "runnable": "agic:chat"},
        "limit": {
            "agic_model_calls": 50,
            "tokens": 1000,
            "cost": "2.5",
        },
    }
    agent = {
        "allow": {"models": [], "skills": ["editor"]},
        "default": {"model": "none"},
        "limit": {"tokens": 2000, "cost": "none", "time": 60},
    }

    assert resolve_resource_filter(
        (root, agent),
        overrides={"models": ("local/*",), "caps": None},
    ) == ResourceFilter(
        models=("local/*",),
        caps=("skill/editor",),
    )
    assert resolve_run_bindings(
        (root, agent),
        overrides={"runnable": None},
    ) == RunBindings(model=None, runnable=None)
    assert resolve_run_limits(
        (root, agent),
        overrides={"time": None},
    ) == RunLimits(
        agic_model_calls=50,
        tokens=2000,
        cost=None,
        time=None,
    )


def test_run_limits_use_limit_table() -> None:
    limits = resolve_run_limits(
        (
            {
                "limit": {
                    "agic_model_calls": 50,
                    "tokens": 1000,
                    "cost": "2.5",
                }
            },
            {"limit": {"tokens": 2000, "cost": "none", "time": 60}},
        )
    )

    assert limits == RunLimits(
        agic_model_calls=50,
        tokens=2000,
        cost=None,
        time=60,
    )


def test_setup_policy_rejects_unknown_and_invalid_fields() -> None:
    with pytest.raises(ValueError, match="unknown allow field: channels"):
        resolve_resource_filter(({"allow": {"channels": ["web"]}},))
    with pytest.raises(TypeError, match="allow models must be an array"):
        resolve_resource_filter(({"allow": {"models": "gateway"}},))
    with pytest.raises(ValueError, match="unknown default field: tool"):
        resolve_run_bindings(({"default": {"tool": "shell"}},))
    with pytest.raises(ValueError, match="unknown run limit: turns"):
        resolve_run_limits(({"limit": {"turns": 1}},))
    with pytest.raises(ValueError, match="cost must be non-negative"):
        resolve_run_limits(({"limit": {"cost": -1.0}},))


def test_old_nested_run_limits_are_not_interpreted() -> None:
    limits = resolve_run_limits(({"run": {"limits": {"tokens": 1000}}},))

    assert limits == RunLimits()
