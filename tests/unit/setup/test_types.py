from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import toolang.setup as setup_package
from toolang.base.types.model import ModelInfo
from toolang.common.layout import AgentLayout
from toolang.setup import AgentSetup


def test_setup_facade_exposes_snapshots_without_cache_details() -> None:
    assert setup_package.__all__ == [
        "AgentSetup",
        "SetupWatcher",
    ]
    assert not hasattr(setup_package, "ModelListCache")
    assert not hasattr(setup_package, "prepare_agent_setup")


def test_agent_setup_copies_and_freezes_implementation_mappings() -> None:
    tools = {"shell": cast(Any, object())}
    providers = {"openai": cast(Any, SimpleNamespace(name="openai"))}
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
    with pytest.raises(TypeError):
        cast(dict[str, object], setup.tools)["other"] = object()


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
                "alias": cast(Any, SimpleNamespace(name="actual")),
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
                "openai": cast(Any, SimpleNamespace(name="openai")),
            },
            adapters={},
            models=(model, model),
            tools={},
            envs={},
        )
