from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.setup import (
    AgentSetup,
    ModelListCache,
    SetupWatcher,
    prepare_agent_setup,
)


@dataclass
class _Provider:
    models: tuple[ModelInfo, ...]
    name: str = "test"
    description: str | None = None

    def required_env_vars(self) -> tuple[str, ...]:
        return ("TEST_API_KEY",)

    def default_base_url(self, *, environ: Mapping[str, str]) -> str | None:
        del environ
        return "https://models.example.test/v1"

    def default_api_key_env(self) -> str | None:
        return "TEST_API_KEY"

    def list_models(self, *, environ: Mapping[str, str]) -> tuple[ModelInfo, ...]:
        del environ
        return self.models

    def prepare_target(self, target: ModelTarget) -> ModelTarget:
        return target


def _model(name: str) -> ModelInfo:
    return ModelInfo(
        ref=f"test/{name}",
        provider="test",
        name=name,
        model=name,
    )


def test_prepare_agent_setup_captures_discovered_models(tmp_path: Path) -> None:
    provider = _Provider((_model("one"),))

    setup = asyncio.run(
        prepare_agent_setup(
            name="alice",
            home=tmp_path / "agents" / "alice",
            providers={"test": provider},
            adapters={"default": cast(ModelAdapter, cast(Any, object()))},
            tools={},
            envs={"TEST_API_KEY": "secret"},
            cache=ModelListCache(tmp_path / ".runtime" / "models"),
        )
    )

    assert setup.models == (_model("one"),)
    assert setup.envs == {"TEST_API_KEY": "secret"}


def test_setup_watcher_observes_external_cache_refresh(tmp_path: Path) -> None:
    provider = _Provider((_model("one"),))
    envs = {"TEST_API_KEY": "secret"}
    cache = ModelListCache(tmp_path / ".runtime" / "models")
    initial = asyncio.run(
        prepare_agent_setup(
            name="alice",
            home=tmp_path / "agents" / "alice",
            providers={"test": provider},
            adapters={"default": cast(ModelAdapter, cast(Any, object()))},
            tools={},
            envs=envs,
            cache=cache,
        )
    )
    watcher = SetupWatcher(initial, cache=cache, get_envs=lambda: envs)
    provider.models = (_model("two"),)
    asyncio.run(cache.get(provider, envs=envs, refresh=True))

    current = asyncio.run(watcher.refresh())

    assert current.models == (_model("two"),)


def test_setup_watcher_rebuilds_snapshot_when_envs_change(tmp_path: Path) -> None:
    provider = _Provider((_model("one"),))
    envs = {"TEST_API_KEY": "first"}
    cache = ModelListCache(tmp_path / ".runtime" / "models")
    initial = AgentSetup(
        name="alice",
        home=tmp_path / "agents" / "alice",
        providers={"test": provider},
        adapters={},
        models=(_model("one"),),
        tools={},
        envs=envs,
    )
    watcher = SetupWatcher(initial, cache=cache, get_envs=lambda: envs)
    envs["TEST_API_KEY"] = "second"

    current = asyncio.run(watcher.refresh())

    assert current.envs == {"TEST_API_KEY": "second"}
