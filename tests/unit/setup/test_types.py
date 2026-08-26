from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

import toolang.setup as setup_package
from toolang.base.types.model import ModelInfo, Provider
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.common.layout import AgentLayout
from toolang.setup import AgentEnvironment, AgentSetup


def test_setup_facade_exposes_snapshots_without_cache_details() -> None:
    assert setup_package.__all__ == [
        "AgentEnvironment",
        "AgentSetup",
        "SetupWatcher",
    ]
    assert not hasattr(setup_package, "ModelListCache")
    assert not hasattr(setup_package, "prepare_agent_setup")


def test_agent_setup_copies_and_freezes_implementation_mappings() -> None:
    tools = {"shell": cast(Any, object())}
    providers = {
        "openai": Provider(
            id="openai",
            name="OpenAI",
            env=(),
            npm="@ai-sdk/openai",
            models={},
        )
    }
    adapters = {"responses": cast(Any, object())}
    environ = {"OPENAI_API_KEY": "secret"}

    setup = AgentSetup(
        layout=AgentLayout.resident(Path("/toolang"), "alice"),
        providers=providers,
        adapters=adapters,
        models=(),
        tools=tools,
        envs=environ,
    )
    tools.clear()
    providers.clear()
    adapters.clear()
    environ.clear()

    assert tuple(setup.tools) == ("shell",)
    assert tuple(setup.providers) == ("openai",)
    assert tuple(setup.adapters) == ("responses",)
    assert setup.envs == {"OPENAI_API_KEY": "secret"}
    assert setup.ceiling == AgentCeiling()
    assert setup.bindings == RunBindings()
    assert setup.limits == RunLimits()
    with pytest.raises(TypeError):
        cast(dict[str, object], setup.tools)["other"] = object()


def test_agent_environment_captures_safe_sandbox_context(
    tmp_path: Path,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")

    environment = AgentEnvironment.capture(
        layout,
        sandbox="docker:python:3.13-slim",
    )

    assert environment.sandbox == "docker:python:3.13-slim"
    assert environment.container is True
    assert environment.root == layout.root
    assert environment.home == layout.home
    assert environment.system
    assert environment.machine


def test_agent_setup_rejects_models_without_installed_provider() -> None:
    with pytest.raises(ValueError, match="unknown providers: missing"):
        AgentSetup(
            layout=AgentLayout.resident(Path("/toolang"), "alice"),
            providers={},
            adapters={},
            models=(
                ModelInfo(
                    ref="missing/one",
                    provider="missing",
                    name="one",
                    model="one",
                ),
            ),
            tools={},
            envs={},
        )


def test_agent_setup_rejects_mismatched_provider_mapping_key() -> None:
    with pytest.raises(
        ValueError,
        match="provider mapping key 'alias' does not match 'actual'",
    ):
        AgentSetup(
            layout=AgentLayout.resident(Path("/toolang"), "alice"),
            providers={
                "alias": Provider(
                    id="actual",
                    name="Actual",
                    env=(),
                    npm="@ai-sdk/openai-compatible",
                    models={},
                ),
            },
            adapters={},
            models=(),
            tools={},
            envs={},
        )


def test_agent_setup_rejects_duplicate_model_identity() -> None:
    model = ModelInfo(
        ref="openai/gpt",
        provider="openai",
        name="gpt",
        model="gpt",
    )

    with pytest.raises(ValueError, match="unique by provider and ref"):
        AgentSetup(
            layout=AgentLayout.resident(Path("/toolang"), "alice"),
            providers={
                "openai": Provider(
                    id="openai",
                    name="OpenAI",
                    env=(),
                    npm="@ai-sdk/openai",
                    models={},
                ),
            },
            adapters={},
            models=(model, model),
            tools={},
            envs={},
        )
