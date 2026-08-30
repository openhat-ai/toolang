from __future__ import annotations

from pathlib import Path

import pytest

from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.lang.runnable_query import RUNNABLE_SCHEMA
from toolang.setup.config import (
    load_agent_config,
    load_setup_config,
    load_setup_envs,
    resolve_agent_ceiling,
    resolve_run_bindings,
    resolve_run_limits,
)
from toolang.state.collections import cap_kind_definition


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


def test_setup_envs_overlay_root_agent_and_process_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (root / ".env").write_text(
        "ROOT_ONLY=from-root\nLAYERED=from-root\nOVERRIDDEN=from-root\n",
        encoding="utf-8",
    )
    (home / ".env").write_text(
        "AGENT_ONLY=from-agent\nLAYERED=from-agent\nOVERRIDDEN=from-agent\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OVERRIDDEN", "from-process")
    monkeypatch.setenv("PROCESS_ONLY", "from-process")

    envs = load_setup_envs(AgentLayout.resident(root, "alice"))

    assert envs["ROOT_ONLY"] == "from-root"
    assert envs["AGENT_ONLY"] == "from-agent"
    assert envs["LAYERED"] == "from-agent"
    assert envs["OVERRIDDEN"] == "from-process"
    assert envs["PROCESS_ONLY"] == "from-process"


def test_setup_envs_treat_dotenv_values_as_literals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.env.write_text("LITERAL='${HOME}/agent'\n", encoding="utf-8")
    monkeypatch.setenv("HOME", "/host/home")

    envs = load_setup_envs(layout)

    assert envs["LITERAL"] == "${HOME}/agent"


def test_setup_policy_overlays_root_agent_and_frozen_overrides() -> None:
    root = {
        "allow": {
            "models": ["gateway/*"],
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

    assert resolve_agent_ceiling(
        (root, agent),
        overrides={"models": ("local/*",), "services": None},
    ) == AgentCeiling(
        models=("local/*",),
        skills=("editor",),
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
        resolve_agent_ceiling(({"allow": {"channels": ["web"]}},))
    with pytest.raises(TypeError, match="allow models must be an array"):
        resolve_agent_ceiling(({"allow": {"models": "gateway"}},))
    with pytest.raises(ValueError, match="unknown default field: tool"):
        resolve_run_bindings(({"default": {"tool": "shell"}},))
    with pytest.raises(ValueError, match="model request ref must be exact"):
        resolve_run_bindings(({"default": {"model": "openai/*"}},))
    with pytest.raises(ValueError, match="unknown run limit: turns"):
        resolve_run_limits(({"limit": {"turns": 1}},))
    with pytest.raises(ValueError, match="cost must be non-negative"):
        resolve_run_limits(({"limit": {"cost": -1.0}},))
    with pytest.raises(ValueError, match="cannot mix queries with all or none"):
        resolve_agent_ceiling(({"allow": {"models": ["all,openai/*"]}},))
    with pytest.raises(ValueError, match="unknown allow field: caps"):
        resolve_agent_ceiling(({"allow": {"caps": ["skill/reviewer"]}},))


def test_setup_policy_validates_owner_collection_queries_when_provided() -> None:
    with pytest.raises(ToolangError, match="unknown skills query field"):
        resolve_agent_ceiling(
            ({"allow": {"skills": ["*[missing=value]"]}},),
            cap_query_schemas={
                name: cap_kind_definition(kind).schema
                for name, kind in (
                    ("psyches", "psyche"),
                    ("skills", "skill"),
                    ("services", "service"),
                    ("prompts", "prompt"),
                )
            },
        )
    with pytest.raises(ToolangError, match="unknown runnables query field"):
        resolve_run_bindings(
            ({"default": {"runnable": "*[missing=value]"}},),
            runnable_query_schema=RUNNABLE_SCHEMA,
        )


def test_setup_policy_all_and_none_are_standalone_layer_values() -> None:
    root = {"allow": {"models": ["openai/*"], "skills": ["review"]}}
    agent = {"allow": {"models": ["all"], "skills": ["none"]}}

    assert resolve_agent_ceiling((root, agent)) == AgentCeiling(skills=())


def test_old_nested_run_limits_are_not_interpreted() -> None:
    limits = resolve_run_limits(({"run": {"limits": {"tokens": 1000}}},))

    assert limits == RunLimits()
