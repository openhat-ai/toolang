from __future__ import annotations

from pathlib import Path

import pytest

from toolang.base.model_settings import parse_model_body
from toolang.base.types.model import (
    ModelRequest,
    Reasoning,
)
from toolang.base.types.policy import AgentCeiling, RunDefaults, RunLimits
from toolang.common.layout import AgentLayout
from toolang.setup.config import (
    load_agent_config,
    load_setup_config,
    load_setup_envs,
    project_model_setup_config,
    resolve_run_defaults,
    resolve_run_limits,
    resolve_setup_allow,
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


def test_setup_envs_select_agent_bindings_from_root_environment(
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
        "ROOT_ONLY\nAGENT_ONLY=from-agent\nLAYERED=from-agent\nOVERRIDDEN=from-agent\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OVERRIDDEN", "from-process")
    monkeypatch.setenv("PROCESS_ONLY", "from-process")

    envs = load_setup_envs(AgentLayout.resident(root, "alice"))

    assert envs["ROOT_ONLY"] == "from-root"
    assert envs["AGENT_ONLY"] == "from-agent"
    assert envs["LAYERED"] == "from-agent"
    assert envs["OVERRIDDEN"] == "from-agent"
    assert "PROCESS_ONLY" not in envs


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


def test_model_setup_projection_excludes_selected_catalog_path() -> None:
    config = {
        "plugin": {
            "model_catalog": {
                "models_dev": {"path": "/host/root/models.json", "max_bytes": 100},
                "company": {"url": "https://catalog.test/models.json"},
            },
            "model_adapter": {"responses": {"profile": "default"}},
        }
    }

    assert project_model_setup_config(config) == {
        "plugin": {
            "model_catalog": {
                "models_dev": {"max_bytes": 100},
                "company": {"url": "https://catalog.test/models.json"},
            },
            "model_adapter": {"responses": {"profile": "default"}},
        }
    }
    assert (
        project_model_setup_config(
            {"plugin": {"model_catalog": {"models_dev": {"path": "/guest/models"}}}}
        )
        == {}
    )


def test_setup_policy_overlays_root_agent_and_frozen_overrides() -> None:
    root = {
        "allow": {
            "models": ["gateway/*"],
            "skills": ["reviewer"],
        },
        "model": {"ref": "gateway/chat"},
        "limit": {
            "agic_model_calls": 50,
            "tokens": 1000,
            "cost": "2.5",
        },
    }
    agent = {
        "allow": {"models": [], "skills": ["editor"]},
        "model": {"ref": "unset"},
        "limit": {"tokens": 2000, "cost": "none", "time": 60},
    }

    assert resolve_setup_allow(
        (root, agent),
        overrides={"models": ("local/*",)},
    ) == AgentCeiling(
        models=("local/*",),
    )
    assert resolve_run_defaults(
        (root, agent),
    ) == RunDefaults(model=None)
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


def test_default_model_body_layers_identity_and_typed_parameters() -> None:
    assert resolve_run_defaults(
        (
            {"model": {"ref": "openai/gpt-5", "effort": "low"}},
            {"model": {"effort": "high"}},
        ),
        overrides={"model": parse_model_body("effort=auto")},
    ) == RunDefaults(model=ModelRequest("openai/gpt-5"))

    assert resolve_run_defaults(
        (
            {"model": {"ref": "openai/gpt-5", "effort": "low"}},
            {"model": {"effort": 4096}},
        )
    ) == RunDefaults(
        model=ModelRequest(
            "openai/gpt-5",
            reasoning=Reasoning(budget_tokens=4096),
        )
    )


def test_setup_policy_rejects_unknown_and_invalid_fields() -> None:
    with pytest.raises(ValueError, match="unknown allow field: channels"):
        resolve_setup_allow(({"allow": {"channels": ["web"]}},))
    with pytest.raises(TypeError, match="allow models must be an array"):
        resolve_setup_allow(({"allow": {"models": 42}},))
    with pytest.raises(ValueError, match="unknown model field: tool"):
        resolve_run_defaults(({"model": {"tool": "shell"}},))
    with pytest.raises(ValueError, match="model request ref must be exact"):
        resolve_run_defaults(({"model": {"ref": "openai/*"}},))
    with pytest.raises(TypeError, match="model.ref must be a string"):
        resolve_run_defaults(({"model": {"ref": {"ref": "openai/gpt-5"}}},))
    with pytest.raises(ValueError, match="unknown run limit: turns"):
        resolve_run_limits(({"limit": {"turns": 1}},))
    with pytest.raises(ValueError, match="cost must be non-negative"):
        resolve_run_limits(({"limit": {"cost": -1.0}},))
    with pytest.raises(ValueError, match="cannot mix queries with all or none"):
        resolve_setup_allow(({"allow": {"models": ["all,openai/*"]}},))
    with pytest.raises(ValueError, match="unknown allow field: caps"):
        resolve_setup_allow(({"allow": {"caps": ["skill/reviewer"]}},))
    with pytest.raises(ValueError, match="unknown Setup allow override: services"):
        resolve_setup_allow((), overrides={"services": None})


def test_setup_policy_validates_owned_collection_queries() -> None:
    assert resolve_setup_allow(
        ({"allow": {"models": ["*[missing=value]"]}},)
    ).models == ("*[missing=value]",)


def test_setup_policy_ignores_state_owned_allow_value_syntax() -> None:
    assert resolve_setup_allow(
        (
            {
                "allow": {
                    "models": ["openai/*"],
                    "skills": "State validates this value",
                }
            },
        )
    ) == AgentCeiling(models=("openai/*",))


def test_setup_policy_all_and_none_are_standalone_layer_values() -> None:
    root = {"allow": {"models": ["openai/*"], "skills": ["review"]}}
    agent = {"allow": {"models": ["all"], "skills": ["none"]}}

    assert resolve_setup_allow((root, agent)) == AgentCeiling()


def test_old_nested_run_limits_are_not_interpreted() -> None:
    limits = resolve_run_limits(({"run": {"limits": {"tokens": 1000}}},))

    assert limits == RunLimits()


def test_structured_model_layers_reset_identity_and_inherit_parameters():
    root = {"model": {"ref": "test/base", "effort": "high", "max_output": 4096}}
    assert resolve_run_defaults(
        (root, {"model": {"effort": "auto"}})
    ).model == ModelRequest("test/base", max_output=4096)
    assert resolve_run_defaults(
        (root, {"model": {"ref": "test/base"}})
    ).model == ModelRequest("test/base")
    assert (
        resolve_run_defaults((root, {"model": {}})).model
        == resolve_run_defaults((root,)).model
    )
    assert resolve_run_defaults((root, {"model": {"ref": "unset"}})).model is None
    with pytest.raises(ValueError, match="effective model"):
        resolve_run_defaults(({"model": {"effort": "high"}},))


@pytest.mark.parametrize("scope", ["model", "talk.model", "compact.model"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("effort", True),
        ("effort", 0.2),
        ("effort", -1),
        ("effort", ""),
        ("ref", 42),
        ("ref", "test/a effort=low"),
        ("max_output", 0),
        ("max_output", False),
        ("max_output", "8192"),
        ("temperature", 0.2),
    ],
)
def test_structured_model_validation_has_source_and_field(
    tmp_path, scope, field, value
):
    from toolang.setup.config import resolve_compact_config, resolve_talk_config
    from toolang.base.types.policy import RunLimits

    config = (
        {scope: {field: value}}
        if "." not in scope
        else {scope.split(".")[0]: {"model": {field: value}}}
    )
    path = tmp_path / "config.toml"
    with pytest.raises((TypeError, ValueError), match=str(path)) as exc:
        if scope == "model":
            resolve_run_defaults((config,), origins=(path,))
        elif scope == "talk.model":
            resolve_talk_config((config,), limits=RunLimits(), origins=(path,))
        else:
            resolve_compact_config((config,), origins=(path,))
    assert "model" in str(exc.value)


@pytest.mark.parametrize("scope", ["root", "agent"])
@pytest.mark.parametrize(
    "value",
    [{}, {"model": "test/old effort=high"}, {"model": "none"}, {"runnable": "work"}],
)
def test_unowned_default_configuration_is_ignored(scope, value):
    configs = [
        {"model": {"ref": "test/base"}},
        {"model": {"max_output": 128}},
    ]
    configs[0 if scope == "root" else 1]["default"] = value
    assert resolve_run_defaults(configs) == RunDefaults(
        model=ModelRequest("test/base", max_output=128)
    )
    assert resolve_run_defaults(({"default": value},)) == RunDefaults()
    assert not hasattr(RunDefaults(), "runnable")


def test_talk_policy_is_sparse_and_above_resolved_general_limits():
    from toolang.setup.config import resolve_talk_config
    from toolang.base.types.policy import RunLimits

    config = resolve_talk_config(
        (
            {
                "talk": {
                    "model": {"ref": "test/router", "effort": "low"},
                    "limit": {"time": 20, "cost": "0.20"},
                }
            },
            {
                "talk": {
                    "model": {"effort": "auto"},
                    "limit": {"tokens": 0, "cost": "none"},
                }
            },
        ),
        limits=RunLimits(agic_model_calls=50, time=500, tokens=9000),
    )
    assert config.model == (
        parse_model_body("test/router effort=low"),
        parse_model_body("effort=auto"),
    )
    assert config.limits == RunLimits(agic_model_calls=50, time=20, tokens=0)
    for field in ("allow", "unknown"):
        with pytest.raises(ValueError, match="talk field"):
            resolve_talk_config(({"talk": {field: {}}},), limits=RunLimits())


@pytest.mark.parametrize(
    "root_talk,agent_talk,expected",
    [
        (
            {},
            {},
            RunLimits(
                agic_model_calls=11, agic_tool_calls=12, tokens=13, cost=14, time=15
            ),
        ),
        (
            {"agic_model_calls": 0, "cost": "0.25"},
            {"agic_tool_calls": "none", "tokens": 0, "time": "none"},
            RunLimits(agic_model_calls=0, tokens=0, cost=0.25),
        ),
        (
            dict.fromkeys(
                ("agic_model_calls", "agic_tool_calls", "tokens", "cost", "time"), 0
            ),
            {},
            RunLimits(agic_model_calls=0, agic_tool_calls=0, tokens=0, cost=0, time=0),
        ),
        (
            {
                "agic_model_calls": 1,
                "agic_tool_calls": 2,
                "tokens": 3,
                "cost": "4.25",
                "time": 5,
            },
            dict.fromkeys(
                ("agic_model_calls", "agic_tool_calls", "tokens", "cost", "time"),
                "none",
            ),
            RunLimits(agic_model_calls=None),
        ),
    ],
)
def test_talk_limits_overlay_every_resolved_ordinary_field(
    root_talk, agent_talk, expected
):
    from toolang.setup.config import resolve_talk_config

    configs = (
        {
            "limit": {
                "agic_model_calls": 1,
                "agic_tool_calls": 2,
                "tokens": 3,
                "cost": "4",
                "time": 5,
            },
            "talk": {"limit": root_talk},
        },
        {
            "limit": {
                "agic_model_calls": 6,
                "agic_tool_calls": 7,
                "tokens": 8,
                "cost": "9",
                "time": 10,
            },
            "talk": {"limit": agent_talk},
        },
    )
    ordinary = resolve_run_limits(
        configs,
        overrides={
            "agic_model_calls": 11,
            "agic_tool_calls": 12,
            "tokens": 13,
            "cost": 14,
            "time": 15,
        },
    )
    assert resolve_talk_config(configs, limits=ordinary).limits == expected
