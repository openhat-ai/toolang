from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any, cast

import pytest

import toolang.setup as setup_package
from toolang.base.types.model import ModelInfo, ModelTarget, Provider
from toolang.base.types.policy import RunDefaults, RunLimits
from toolang.common.layout import AgentLayout
from toolang.setup import (
    AgentEnvironment,
    AgentSetup,
    ModelCollection,
    ModelEntry,
    ToolCollection,
)


def test_setup_facade_exposes_snapshots_without_cache_details() -> None:
    assert setup_package.__all__ == [
        "AgentEnvironment",
        "AgentSetup",
        "ModelCollection",
        "ModelEntry",
        "RunDefaults",
        "SetupWatcher",
        "ToolCollection",
        "ToolEntry",
    ]
    assert not hasattr(setup_package, "ModelListCache")
    assert not hasattr(setup_package, "prepare_agent_setup")


def test_agent_setup_has_only_effective_publication_fields() -> None:
    assert tuple(item.name for item in fields(AgentSetup)) == (
        "layout",
        "providers",
        "adapters",
        "models",
        "tools",
        "envs",
        "environment",
        "defaults",
        "limits",
    )


def test_run_defaults_require_a_typed_model_request() -> None:
    with pytest.raises(TypeError, match="run default model must be a ModelRequest"):
        RunDefaults(model=cast(Any, "test/model"))


def test_agent_setup_copies_and_freezes_implementation_mappings() -> None:
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
        models=ModelCollection(),
        tools=ToolCollection(),
        envs=environ,
    )
    providers.clear()
    adapters.clear()
    environ.clear()

    assert tuple(setup.tools) == ()
    assert tuple(setup.providers) == ("openai",)
    assert tuple(setup.adapters) == ("responses",)
    assert setup.envs == {"OPENAI_API_KEY": "secret"}
    assert setup.defaults == RunDefaults()
    assert setup.limits == RunLimits()
    with pytest.raises(TypeError):
        cast(dict[str, object], setup.providers)["other"] = object()


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
    info = ModelInfo(
        ref="missing/one",
        provider="missing",
        name="one",
        model="one",
    )
    with pytest.raises(ValueError, match="unknown providers: missing"):
        AgentSetup(
            layout=AgentLayout.resident(Path("/toolang"), "alice"),
            providers={},
            adapters={},
            models=ModelCollection(
                (
                    ModelEntry(
                        key=info.ref,
                        ref=info.ref,
                        target=ModelTarget(
                            ref=info.ref,
                            provider=info.provider,
                            name=info.name,
                            model=info.model,
                            adapter="test",
                        ),
                        info=info,
                    ),
                )
            ),
            tools=ToolCollection(),
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
            models=ModelCollection(),
            tools=ToolCollection(),
            envs={},
        )


def test_model_collection_rejects_duplicate_public_ref() -> None:
    model = ModelInfo(
        ref="openai/gpt",
        provider="openai",
        name="gpt",
        model="gpt",
    )

    entry = ModelEntry(
        key=model.ref,
        ref=model.ref,
        target=ModelTarget(
            ref=model.ref,
            provider=model.provider,
            name=model.name,
            model=model.model,
            adapter="test",
        ),
        info=model,
    )

    with pytest.raises(ValueError, match="duplicate entry keys"):
        ModelCollection((entry, entry))
