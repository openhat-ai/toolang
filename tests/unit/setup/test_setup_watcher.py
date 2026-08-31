from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pytest

from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.common.errors import ToolangError
from toolang.base.types.model import Model, ModelCatalogSnapshot, ModelInfo, Provider
from toolang.common.layout import AgentLayout
from toolang.plugin.models.adapters.responses import ResponsesModelAdapter
from toolang.plugin.models.adapters.chat_completions import (
    ChatCompletionsModelAdapter,
)
from toolang.plugin.models.catalog import ModelsDevModelCatalog
from toolang.plugin.models.local import LlamaCppModelCatalog, OllamaModelCatalog
from toolang.setup import SetupWatcher
from toolang.setup import watcher as watcher_module


class _Tool:
    name = "one"
    plugin_name = "test"
    toolset = "alpha"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description="Use alpha one.")

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del arguments, context
        return {}


def test_setup_watcher_current_requires_initial_refresh(tmp_path: Path) -> None:
    watcher = SetupWatcher(AgentLayout.resident(tmp_path, "alice"))

    with pytest.raises(RuntimeError, match="has not been refreshed"):
        watcher.current()


def test_setup_watcher_loads_catalog_without_persistent_model_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one", "two"))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})

    setup = asyncio.run(watcher.refresh())

    assert tuple(setup.providers) == ("test",)
    assert setup.models.refs() == ("test/one", "test/two")
    assert all(entry.target.adapter == "responses" for entry in setup.models.entries)
    assert not (tmp_path / ".setup" / "models").exists()


def test_setup_watcher_keeps_runtime_sandbox_separate_from_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one",))
    watcher = _watcher(
        monkeypatch,
        tmp_path,
        envs={"TOOLANG_SANDBOX": "docker:spoofed"},
    )

    setup = asyncio.run(watcher.refresh())
    docker_setup = asyncio.run(
        SetupWatcher(
            AgentLayout.resident(tmp_path, "alice"),
            sandbox="docker:python:3.13-slim",
        ).refresh()
    )

    assert setup.envs["TOOLANG_SANDBOX"] == "docker:spoofed"
    assert setup.environment is not None
    assert setup.environment.sandbox == "host"
    assert docker_setup.environment is not None
    assert docker_setup.environment.sandbox == "docker:python:3.13-slim"


def test_setup_watcher_reuses_static_parse_and_reprobes_local_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one",))
    parse_calls = 0
    local_calls = 0
    model_info_calls = 0
    original_snapshot = ModelsDevModelCatalog.snapshot
    original_model_info = watcher_module.model_info_from_catalog

    async def count_parse(self: ModelsDevModelCatalog) -> ModelCatalogSnapshot:
        nonlocal parse_calls
        parse_calls += 1
        return await original_snapshot(self)

    def count_model_info(model: Model) -> ModelInfo:
        nonlocal model_info_calls
        model_info_calls += 1
        return original_model_info(model)

    async def count_local(self: object) -> ModelCatalogSnapshot:
        nonlocal local_calls
        local_calls += 1
        provider_id = (
            "ollama" if self.__class__.__name__ == "OllamaModelCatalog" else "llama_cpp"
        )
        return _empty_local(provider_id)

    monkeypatch.setattr(ModelsDevModelCatalog, "snapshot", count_parse)
    monkeypatch.setattr(OllamaModelCatalog, "snapshot", count_local)
    monkeypatch.setattr(LlamaCppModelCatalog, "snapshot", count_local)
    monkeypatch.setattr(watcher_module, "model_info_from_catalog", count_model_info)
    watcher = _watcher(
        monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"}, patch_local=False
    )

    first = asyncio.run(watcher.refresh())
    second = asyncio.run(watcher.refresh())
    forced = asyncio.run(watcher.refresh(force=True))

    assert first is second
    assert forced is not first
    assert parse_calls == 1
    assert local_calls == 6
    assert model_info_calls == 2


def test_setup_watcher_detects_local_models_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one",))
    calls = 0

    async def changing_ollama(_self: object) -> ModelCatalogSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _empty_local("ollama")
        model = Model(
            provider_id="ollama",
            id="new-local",
            name="New Local",
            local=True,
        )
        provider = Provider(
            id="ollama",
            name="Ollama",
            env=(),
            npm="@ai-sdk/openai-compatible",
            api="http://127.0.0.1:11434/v1",
            models={model.id: model},
            local=True,
        )
        return ModelCatalogSnapshot(
            providers={provider.id: provider},
            models=(model,),
            revision=f"runtime:ollama:{calls}",
        )

    monkeypatch.setattr(OllamaModelCatalog, "snapshot", changing_ollama)

    async def empty_llama(_self: object) -> ModelCatalogSnapshot:
        return _empty_local("llama_cpp")

    monkeypatch.setattr(LlamaCppModelCatalog, "snapshot", empty_llama)
    watcher = _watcher(
        monkeypatch,
        tmp_path,
        envs={"TEST_API_KEY": "secret"},
        patch_local=False,
    )

    first = asyncio.run(watcher.refresh())
    second = asyncio.run(watcher.refresh(force=True))

    assert first is not second
    assert second.models.contains("ollama/new-local")


def test_setup_watcher_rebuilds_when_selected_catalog_file_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models.json"
    _write_catalog(path, ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    first = asyncio.run(watcher.refresh())

    _write_catalog(path, ("one", "second"))
    second = asyncio.run(watcher.refresh())

    assert first.models.refs() == ("test/one",)
    assert second.models.refs() == ("test/one", "test/second")


def test_setup_watcher_rebuilds_when_environment_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one",))
    envs = {"TEST_API_KEY": "first"}
    watcher = _watcher(monkeypatch, tmp_path, envs=envs)
    first = asyncio.run(watcher.refresh())

    envs["TEST_API_KEY"] = "second"
    second = asyncio.run(watcher.refresh())

    assert first is not second
    assert second.envs["TEST_API_KEY"] == "second"


def test_setup_watcher_failed_refresh_keeps_last_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models.json"
    _write_catalog(path, ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    expected = asyncio.run(watcher.refresh())
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid model catalog JSON"):
        asyncio.run(watcher.refresh())

    assert watcher.current() is expected


def test_setup_watcher_routes_only_each_plugins_canonical_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one",))
    root_config = {
        "plugin": {
            "toolset": {"fs": {"max_chars": 1000}},
            "model_adapter": {"responses": {"credential_env": "ADAPTER_TOKEN"}},
            "model_catalog": {"ollama": {"timeout": 3}},
        }
    }
    agent_config = {
        "plugin": {
            "toolset": {"fs": {"max_chars": 2000}},
            "model_adapter": {"responses": {"profile": "agent"}},
        }
    }
    adapter_calls: list[dict[str, dict[str, object]]] = []
    toolset_calls: list[dict[str, dict[str, object]]] = []
    catalog_calls: list[dict[str, dict[str, object]]] = []
    original_catalog_loader = watcher_module.load_model_catalogs

    monkeypatch.setattr(
        watcher_module,
        "load_setup_config",
        lambda _layout: root_config,
    )
    monkeypatch.setattr(
        watcher_module,
        "load_agent_config",
        lambda _layout: agent_config,
    )
    monkeypatch.setattr(
        watcher_module,
        "load_setup_envs",
        lambda _layout: {
            "TEST_API_KEY": "secret",
        },
    )

    def load_adapters(
        config: dict[str, dict[str, object]],
    ) -> dict[str, ResponsesModelAdapter]:
        adapter_calls.append(config)
        return {"responses": ResponsesModelAdapter()}

    def load_tools(
        *, toolset_config: dict[str, dict[str, object]]
    ) -> dict[str, object]:
        toolset_calls.append(toolset_config)
        return {}

    def load_catalogs(
        config: dict[str, dict[str, object]],
    ) -> dict[str, ModelCatalog]:
        catalog_calls.append(config)
        return original_catalog_loader(config)

    monkeypatch.setattr(watcher_module, "load_model_adapters", load_adapters)
    monkeypatch.setattr(watcher_module, "load_tools", load_tools)
    monkeypatch.setattr(watcher_module, "load_model_catalogs", load_catalogs)

    asyncio.run(SetupWatcher(AgentLayout.resident(tmp_path, "alice")).refresh())

    assert adapter_calls == [
        {
            "responses": {
                "credential_env": "ADAPTER_TOKEN",
                "profile": "agent",
            }
        }
    ]
    assert toolset_calls == [{"fs": {"max_chars": 2000}}]
    assert catalog_calls[0]["ollama"]["timeout"] == 3
    assert "root" not in catalog_calls[0]["ollama"]
    assert "mode" not in catalog_calls[0]["ollama"]


def test_setup_watcher_publishes_only_effective_resources_and_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one", "two"))
    root_config = {
        "allow": {"models": ["test/*"], "tools": ["none"]},
        "default": {"model": "test/two", "runnable": "agic:chat"},
        "limit": {"tokens": 100, "cost": "1.5"},
    }
    agent_config = {
        "allow": {"models": ["test/two"], "tools": ["none"]},
        "limit": {"tokens": 200},
    }
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    monkeypatch.setattr(
        watcher_module, "load_setup_config", lambda _layout: root_config
    )
    monkeypatch.setattr(
        watcher_module, "load_agent_config", lambda _layout: agent_config
    )
    monkeypatch.setattr(
        watcher_module,
        "load_tools",
        lambda **_kwargs: {"alpha__one": _Tool()},
    )
    watcher = SetupWatcher(
        watcher.layout,
        allow_overrides={"models": ("test/one",), "tools": ("alpha/*",)},
        default_overrides={"model": "test/one"},
        limit_overrides={"tokens": 300},
    )

    setup = asyncio.run(watcher.refresh())

    assert setup.models.refs() == ("test/one",)
    assert setup.tools.refs() == ("alpha/one",)
    assert tuple(setup.providers) == ("test",)
    assert tuple(setup.providers["test"].models) == ("one",)
    assert setup.defaults.model == "test/one"
    assert setup.defaults.runnable == "agic:chat"
    assert setup.limits.tokens == 300
    assert setup.limits.cost == Decimal("1.5")
    assert not hasattr(setup, "allow")
    assert not hasattr(setup, "catalog")
    assert not hasattr(setup, "ceiling")
    assert not hasattr(setup, "bindings")
    assert not hasattr(setup, "provider_configs")


def test_setup_watcher_keeps_missing_default_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})

    setup = asyncio.run(watcher.refresh())

    assert setup.defaults.model is None


def test_setup_watcher_rejects_default_excluded_from_effective_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one", "two"))
    config = {
        "allow": {"models": ["test/one"]},
        "default": {"model": "test/two"},
    }
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    monkeypatch.setattr(watcher_module, "load_setup_config", lambda _layout: config)

    with pytest.raises(ToolangError, match="model ref is unavailable: test/two"):
        asyncio.run(watcher.refresh())


def test_setup_watcher_retains_last_setup_when_default_becomes_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models.json"
    _write_catalog(path, ("one", "two"))
    config = {"default": {"model": "test/two"}}
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    monkeypatch.setattr(watcher_module, "load_setup_config", lambda _layout: config)
    initial = asyncio.run(watcher.refresh())
    _write_catalog(path, ("one",))

    with pytest.raises(ToolangError, match="model ref is unavailable: test/two"):
        asyncio.run(watcher.refresh())

    assert watcher.current() is initial


def test_setup_watcher_reuses_publication_for_state_only_config_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one",))
    config: dict[str, object] = {"allow": {"prompts": ["prompt/one"]}}
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    monkeypatch.setattr(watcher_module, "load_setup_config", lambda _layout: config)
    initial = asyncio.run(watcher.refresh())
    config = {"allow": {"prompts": ["prompt/two"]}}
    model_info_calls = 0
    original_model_info = watcher_module.model_info_from_catalog

    def count_model_info(model: Model) -> ModelInfo:
        nonlocal model_info_calls
        model_info_calls += 1
        return original_model_info(model)

    monkeypatch.setattr(watcher_module, "model_info_from_catalog", count_model_info)
    monkeypatch.setattr(
        watcher_module,
        "load_model_adapters",
        lambda _config: (_ for _ in ()).throw(
            AssertionError("State-only config must not reload Setup adapters")
        ),
    )
    monkeypatch.setattr(
        watcher_module,
        "load_tools",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("State-only config must not reload Setup tools")
        ),
    )

    refreshed = asyncio.run(watcher.refresh())

    assert refreshed is initial
    assert model_info_calls == 0


def _watcher(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    envs: dict[str, str],
    patch_local: bool = True,
) -> SetupWatcher:
    monkeypatch.setattr(watcher_module, "load_setup_config", lambda _layout: {})
    monkeypatch.setattr(watcher_module, "load_agent_config", lambda _layout: {})
    monkeypatch.setattr(watcher_module, "load_setup_envs", lambda _layout: dict(envs))
    monkeypatch.setattr(
        watcher_module,
        "load_model_adapters",
        lambda _config: {
            "chat_completions": ChatCompletionsModelAdapter(),
            "responses": ResponsesModelAdapter(),
        },
    )
    monkeypatch.setattr(watcher_module, "load_tools", lambda **_kwargs: {})
    if patch_local:

        async def empty_ollama(_self: object) -> ModelCatalogSnapshot:
            return _empty_local("ollama")

        async def empty_llama(_self: object) -> ModelCatalogSnapshot:
            return _empty_local("llama_cpp")

        monkeypatch.setattr(OllamaModelCatalog, "snapshot", empty_ollama)
        monkeypatch.setattr(LlamaCppModelCatalog, "snapshot", empty_llama)
    return SetupWatcher(AgentLayout.resident(root, "alice"))


def _empty_local(provider_id: str) -> ModelCatalogSnapshot:
    provider = Provider(
        id=provider_id,
        name=provider_id,
        env=(),
        npm="@ai-sdk/openai-compatible",
        models={},
        local=True,
    )
    return ModelCatalogSnapshot(
        providers={provider_id: provider},
        models=(),
        revision=f"runtime:{provider_id}",
    )


def _write_catalog(path: Path, model_ids: tuple[str, ...]) -> None:
    models = {
        model_id: {
            "id": model_id,
            "name": model_id.title(),
            "attachment": False,
            "reasoning": False,
            "tool_call": True,
            "structured_output": True,
            "temperature": True,
            "release_date": "2026-01-01",
            "last_updated": "2026-01-01",
            "modalities": {"input": ["text"], "output": ["text"]},
            "open_weights": False,
            "limit": {"context": 1000, "output": 100},
            "cost": {"input": 1, "output": 2},
        }
        for model_id in model_ids
    }
    path.write_text(
        json.dumps(
            {
                "test": {
                    "id": "test",
                    "name": "Test",
                    "env": ["TEST_API_KEY"],
                    "npm": "@ai-sdk/openai",
                    "models": models,
                }
            }
        ),
        encoding="utf-8",
    )
