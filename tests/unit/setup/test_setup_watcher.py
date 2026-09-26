"""SetupWatcher publishes setup revisions with per-instance lazy accessors."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

import pytest

from toolang.base.errors import ToolangError
from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import CatalogModel, CatalogProvider, CatalogSnapshot
from toolang.common.layout import AgentLayout
from toolang.plugin.adapters.chat_completions import ChatCompletionsModelAdapter
from toolang.plugin.adapters.responses import ResponsesModelAdapter
from toolang.plugin.models.query import filter_models
from toolang.plugin.catalogs.models_dev.catalog import ModelsDevModelCatalog
from toolang.setup import watcher as watcher_module
from toolang.setup.watcher import SetupWatcher


def _write_catalog(path: Path, names: tuple[str, ...] = ("one", "two")) -> None:
    provider = {
        "id": "test",
        "name": "Test",
        "env": ["TEST_API_KEY"],
        "npm": "@ai-sdk/openai-compatible",
        "api": "https://example.test/v1",
    }
    models = [
        {
            "id": name,
            "provider": "test",
            "name": name.title(),
            "tool_call": True,
            "modalities": {"input": ["text"], "output": ["text"]},
            "limit": {"context": 1000, "output": 100},
        }
        for name in names
    ]
    path.write_text(
        json.dumps({"providers": [provider], "models": models}), encoding="utf-8"
    )


class _DynamicCatalog:
    name = "dynamic"

    def __init__(self) -> None:
        self.calls = 0
        self.value = CatalogSnapshot(
            providers={
                "dynamic": CatalogProvider(
                    "dynamic",
                    "Dynamic",
                    api="https://dynamic.test/v1",
                    adapter="chat_completions",
                )
            },
            models=(CatalogModel("initial", "Initial", provider_id="dynamic"),),
            revision="constant-plugin-revision",
        )

    async def snapshot(self):
        self.calls += 1
        return self.value


def _watcher(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    envs: dict[str, str] | None = None,
    dynamic: _DynamicCatalog | None = None,
    validate_defaults: bool = False,
) -> tuple[SetupWatcher, dict[str, int]]:
    path = root / "catalog.json"
    if not path.exists():
        _write_catalog(path)
    layout = AgentLayout.resident(root, "alice")
    counts = {"adapters": 0, "toolsets": 0, "catalog_plugins": 0}
    captured_env = envs if envs is not None else {"TEST_API_KEY": "synthetic"}
    monkeypatch.setattr(
        watcher_module, "load_root_setup_envs", lambda _layout: dict(captured_env)
    )
    monkeypatch.setattr(
        watcher_module, "load_setup_envs", lambda _layout: dict(captured_env)
    )
    counter_lock = Lock()

    def load_adapters(_config):
        with counter_lock:
            counts["adapters"] += 1
        return {
            "chat_completions": ChatCompletionsModelAdapter(),
            "responses": ResponsesModelAdapter(),
        }

    def load_catalogs(config):
        counts["catalog_plugins"] += 1
        plugins: dict[str, ModelCatalog] = {
            "models_dev": ModelsDevModelCatalog(Path(config["models_dev"]["path"]))
        }
        if dynamic is not None:
            plugins["dynamic"] = dynamic
        return plugins

    def load_toolsets(**_kwargs):
        counts["toolsets"] += 1
        return {}

    monkeypatch.setattr(watcher_module, "load_model_adapters", load_adapters)
    monkeypatch.setattr(watcher_module, "load_model_catalogs", load_catalogs)
    monkeypatch.setattr(watcher_module, "load_toolsets_with_sources", load_toolsets)
    return (
        SetupWatcher(
            layout,
            model_catalog=path,
            agent_context=False,
            validate_defaults=validate_defaults,
        ),
        counts,
    )


def test_current_requires_initial_refresh(tmp_path: Path) -> None:
    watcher = SetupWatcher(AgentLayout.resident(tmp_path, "alice"), agent_context=False)
    with pytest.raises(RuntimeError, match="not been refreshed"):
        watcher.current()


def test_refresh_publishes_without_loading_adapters_tools_or_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher, counts = _watcher(monkeypatch, tmp_path)
    setup = asyncio.run(watcher.refresh())
    assert setup is watcher.current()
    assert counts == {"adapters": 0, "toolsets": 0, "catalog_plugins": 1}
    assert not (tmp_path / ".setup" / "models").exists()


def test_model_provider_and_effective_accessors_share_one_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher, counts = _watcher(monkeypatch, tmp_path)
    setup = asyncio.run(watcher.refresh())
    with ThreadPoolExecutor(max_workers=8) as pool:
        models = tuple(pool.map(lambda _: setup.models(), range(8)))
    assert all(item is models[0] for item in models)
    assert {model.ref for model in models[0]} == {"test/one", "test/two"}
    assert {provider.id for provider in setup.providers()} == {"test"}
    assert tuple(model.ref for model in setup.models_effective()) == tuple(
        model.ref for model in models[0]
    )
    assert setup.models_effective() is setup.models_effective()
    assert {provider.id for provider in setup.providers_effective()} == {"test"}
    assert setup.model_allowed("test/one")
    assert counts["adapters"] == 1
    assert not (tmp_path / ".setup" / "models").exists()


def test_cached_model_views_are_immutable_reference_sequences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher, _ = _watcher(monkeypatch, tmp_path)
    setup = asyncio.run(watcher.refresh())
    all_models = setup.models()
    ready_models = setup.models_effective()
    all_providers = setup.providers()
    ready_providers = setup.providers_effective()

    assert all(
        isinstance(view, tuple)
        for view in (all_models, ready_models, all_providers, ready_providers)
    )
    assert ready_models[0] is all_models[0]
    assert ready_providers[0] is all_providers[0]
    assert setup.models() is all_models
    assert setup.models_effective() is ready_models


def test_plugin_accessors_load_independently_of_model_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher, counts = _watcher(monkeypatch, tmp_path)
    setup = asyncio.run(watcher.refresh())
    assert not setup.tools()
    assert not setup.toolsets()
    assert "responses" in setup.adapters()
    assert "models_dev" in setup.catalogs()
    assert counts == {"adapters": 1, "toolsets": 1, "catalog_plugins": 2}
    assert not (tmp_path / ".setup" / "models").exists()


def test_effective_views_filter_allow_and_readiness_but_full_views_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "catalog.json"
    _write_catalog(path)
    data = json.loads(path.read_text())
    test_provider = dict(data["providers"][0])
    offline_provider = {**test_provider, "id": "offline", "env": ["MISSING_KEY"]}
    data["providers"].append(offline_provider)
    data["models"].extend(
        {**model, "provider": "offline"}
        for model in tuple(data["models"])
        if model["provider"] == "test"
    )
    path.write_text(json.dumps(data), encoding="utf-8")
    watcher, _counts = _watcher(monkeypatch, tmp_path)
    watcher.layout.root_config.write_text(
        '[allow]\nmodels = ["test/one"]\n', encoding="utf-8"
    )
    setup = asyncio.run(watcher.refresh())
    assert {model.ref for model in setup.models()} == {
        "test/one",
        "test/two",
        "offline/one",
        "offline/two",
    }
    assert tuple(model.ref for model in setup.models_effective()) == ("test/one",)
    assert {provider.id for provider in setup.providers()} == {"test", "offline"}
    assert {provider.id for provider in setup.providers_effective()} == {"test"}
    offline_model = next(
        model for model in setup.models() if model.ref == "offline/one"
    )
    assert not offline_model._toolang.routable
    assert not offline_model._toolang.allowed
    assert setup.model_allowed("test/one")
    assert not setup.model_allowed("test/two")


def test_new_setup_is_lazy_and_old_setup_stays_pinned_after_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "catalog.json"
    _write_catalog(path, ("one",))
    watcher, counts = _watcher(monkeypatch, tmp_path)
    first = asyncio.run(watcher.refresh())
    _write_catalog(path, ("two",))
    second = asyncio.run(watcher.refresh())
    assert second is not first
    assert counts["adapters"] == 0
    assert {model.ref for model in first.models()} == {"test/one"}
    assert {model.ref for model in second.models()} == {"test/two"}
    assert counts["adapters"] == 2


def test_dynamic_probe_changes_publish_new_revision_without_disk_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dynamic = _DynamicCatalog()
    watcher, _counts = _watcher(monkeypatch, tmp_path, dynamic=dynamic)
    first = asyncio.run(watcher.refresh())
    assert asyncio.run(watcher.refresh()) is first
    dynamic.value = CatalogSnapshot(
        providers=dynamic.value.providers,
        models=(CatalogModel("changed", "Changed", provider_id="dynamic"),),
        revision="constant-plugin-revision",
    )
    second = asyncio.run(watcher.refresh())
    assert second is not first
    assert "dynamic/changed" in {model.ref for model in second.models()}
    assert not (tmp_path / ".setup" / "models").exists()


def test_touching_flat_catalog_without_content_change_preserves_setup_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "catalog.json"
    _write_catalog(path, ("one",))
    watcher, _counts = _watcher(monkeypatch, tmp_path)
    first = asyncio.run(watcher.refresh())
    stat = path.stat()
    path.touch()
    assert path.stat().st_mtime_ns != stat.st_mtime_ns

    second = asyncio.run(watcher.refresh())

    assert second is first
    assert second.revision == first.revision


def test_invalid_catalog_refresh_keeps_last_good_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "catalog.json"
    _write_catalog(path, ("one",))
    watcher, _counts = _watcher(monkeypatch, tmp_path)
    good = asyncio.run(watcher.refresh())
    path.write_text("not json", encoding="utf-8")
    assert asyncio.run(watcher.refresh()) is good
    assert watcher.diagnostics()


def test_model_defaults_are_validated_when_models_are_first_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "catalog.json"
    _write_catalog(path, ("one",))
    watcher, _counts = _watcher(monkeypatch, tmp_path, validate_defaults=True)
    watcher.layout.root_config.write_text(
        '[default]\nmodel = "test/missing"\n', encoding="utf-8"
    )
    setup = asyncio.run(watcher.refresh())
    with pytest.raises(ToolangError, match="model ref is unavailable: test/missing"):
        setup.models_effective()


def test_environment_changes_publish_a_new_lazy_setup_and_pin_old_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envs: dict[str, str] = {}
    watcher, counts = _watcher(monkeypatch, tmp_path, envs=envs)
    first = asyncio.run(watcher.refresh())
    assert not first.models_effective()
    first_all = first.models()
    assert not first_all[0]._toolang.routable

    envs["TEST_API_KEY"] = "synthetic"
    second = asyncio.run(watcher.refresh())
    assert second is not first
    assert counts["adapters"] == 1
    assert {model.ref for model in second.models_effective()} == {
        "test/one",
        "test/two",
    }
    assert not first.models_effective()
    assert first.models() is first_all
    assert counts["adapters"] == 2


def test_full_routes_are_resolved_before_effective_adapter_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "catalog.json"
    _write_catalog(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["providers"][0]["npm"] = "@ai-sdk/openai"
    model_two = next(
        model
        for model in raw["models"]
        if model["provider"] == "test" and model["id"] == "two"
    )
    model_two["override"] = {
        "shape": "chat_completions",
        "api": "https://models.example/v1",
    }
    path.write_text(json.dumps(raw), encoding="utf-8")
    watcher, _counts = _watcher(monkeypatch, tmp_path)
    watcher.layout.root_config.write_text(
        '[allow]\nmodels = ["*[adapter=responses]"]\n', encoding="utf-8"
    )
    setup = asyncio.run(watcher.refresh())

    full = setup.models()
    assert tuple(
        model.ref for model in filter_models(full, ("*[adapter=responses]",))
    ) == ("test/one",)
    assert tuple(
        model.ref
        for model in filter_models(full, ("*[route.adapter=chat_completions]",))
    ) == ("test/two",)
    assert tuple(model.ref for model in setup.models_effective()) == ("test/one",)
    assert {provider.id for provider in setup.providers_effective()} == {"test"}
