from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from toolang.base.types.model import Model, ModelCatalogSnapshot, Provider
from toolang.common.layout import AgentLayout
from toolang.plugin.models.adapters.responses import ResponsesModelAdapter
from toolang.plugin.models.catalog import ModelsDevModelCatalog
from toolang.plugin.models.local import LlamaCppModelCatalog, OllamaModelCatalog
from toolang.setup import SetupWatcher
from toolang.setup import watcher as watcher_module


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

    assert setup.catalog is not None
    assert setup.catalog.source == (tmp_path / "models.json").resolve()
    assert tuple(setup.catalog.providers)[:1] == ("test",)
    assert [model.ref for model in setup.models if model.provider == "test"] == [
        "test/one",
        "test/two",
    ]
    assert all(
        model.adapter == "responses"
        for model in setup.models
        if model.provider == "test"
    )
    assert not (tmp_path / ".setup" / "models").exists()


def test_setup_watcher_reuses_static_parse_and_reprobes_local_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one",))
    parse_calls = 0
    local_calls = 0
    original = ModelsDevModelCatalog.snapshot

    async def count_parse(self: ModelsDevModelCatalog) -> ModelCatalogSnapshot:
        nonlocal parse_calls
        parse_calls += 1
        return await original(self)

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
    second = asyncio.run(watcher.refresh())

    assert first is not second
    assert second.catalog is not None
    assert second.catalog.find("ollama", "new-local") is not None


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

    assert first.catalog is not None and second.catalog is not None
    assert first.catalog.revision != second.catalog.revision
    assert [
        item.id for item in second.catalog.models if item.provider_id == "test"
    ] == [
        "one",
        "second",
    ]


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
        lambda: {"responses": ResponsesModelAdapter()},
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
