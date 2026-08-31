from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
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
from toolang.setup import catalog as catalog_module
from toolang.setup import watcher as watcher_module
from toolang.setup.catalog import load_catalog_inspection
from toolang.setup.watcher import DEFAULT_INTERVAL_MS


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


def test_setup_watcher_persists_secret_free_model_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one", "two"))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})

    setup = asyncio.run(watcher.refresh())
    asyncio.run(watcher.refresh())

    assert tuple(setup.providers) == ("test",)
    assert setup.models.refs() == ("test/one", "test/two")
    assert all(entry.target.adapter == "responses" for entry in setup.models.entries)
    cache = tmp_path / ".setup" / "models" / "projection.json"
    assert cache.is_file()
    assert "secret" not in cache.read_text(encoding="utf-8")


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
    adapter_loads = 0
    tool_loads = 0
    catalog_loads = 0
    root_config_loads = 0
    agent_config_loads = 0
    env_loads = 0
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
    original_root_loader = watcher_module.load_setup_config
    original_agent_loader = watcher_module.load_agent_config
    original_env_loader = watcher_module.load_setup_envs
    original_adapter_loader = watcher_module.load_model_adapters
    original_tool_loader = watcher_module.load_tools
    original_catalog_loader = watcher_module.load_model_catalogs

    def count_root_config(layout: AgentLayout) -> dict[str, object]:
        nonlocal root_config_loads
        root_config_loads += 1
        return original_root_loader(layout)

    def count_agent_config(layout: AgentLayout) -> dict[str, object]:
        nonlocal agent_config_loads
        agent_config_loads += 1
        return original_agent_loader(layout)

    def count_envs(layout: AgentLayout) -> dict[str, str]:
        nonlocal env_loads
        env_loads += 1
        return original_env_loader(layout)

    def count_adapters(
        config: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> object:
        nonlocal adapter_loads
        adapter_loads += 1
        return original_adapter_loader(config)

    def count_tools(
        *,
        toolset_config: Mapping[str, Mapping[str, Any]] | None = None,
        queries: Sequence[str] | None = None,
    ) -> object:
        nonlocal tool_loads
        tool_loads += 1
        return original_tool_loader(toolset_config=toolset_config, queries=queries)

    def count_catalogs(config: Mapping[str, Mapping[str, Any]]) -> object:
        nonlocal catalog_loads
        catalog_loads += 1
        return original_catalog_loader(config)

    monkeypatch.setattr(watcher_module, "load_model_adapters", count_adapters)
    monkeypatch.setattr(watcher_module, "load_tools", count_tools)
    monkeypatch.setattr(watcher_module, "load_model_catalogs", count_catalogs)
    monkeypatch.setattr(watcher_module, "load_setup_config", count_root_config)
    monkeypatch.setattr(watcher_module, "load_agent_config", count_agent_config)
    monkeypatch.setattr(watcher_module, "load_setup_envs", count_envs)

    first = asyncio.run(watcher.refresh())
    second = asyncio.run(watcher.refresh())
    third = asyncio.run(watcher.refresh())

    assert first is second
    assert third is first
    assert parse_calls == 1
    assert local_calls == 6
    assert model_info_calls == 1
    assert adapter_loads == 1
    assert tool_loads == 1
    assert catalog_loads == 1
    assert root_config_loads == 1
    assert agent_config_loads == 1
    assert env_loads == 1

    (tmp_path / "config.toml").write_text(
        '[allow]\npsyches = ["psyche/one"]\n',
        encoding="utf-8",
    )
    fourth = asyncio.run(watcher.refresh())

    assert fourth is first
    assert local_calls == 8
    assert root_config_loads == 2
    assert agent_config_loads == 1
    assert env_loads == 1


def test_setup_watcher_warm_process_reuses_persistent_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one", "two"))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    expected = asyncio.run(watcher.refresh())
    asyncio.run(watcher.refresh())

    async def reject_static(_self: object) -> ModelCatalogSnapshot:
        raise AssertionError("warm refresh must not reread the source catalog")

    def reject_projection(_model: Model) -> ModelInfo:
        raise AssertionError("warm refresh must reuse the derived projection")

    monkeypatch.setattr(ModelsDevModelCatalog, "snapshot", reject_static)
    monkeypatch.setattr(watcher_module, "model_info_from_catalog", reject_projection)
    warm = SetupWatcher(AgentLayout.resident(tmp_path, "alice"))

    actual = asyncio.run(warm.refresh())

    assert actual.models.refs() == expected.models.refs()


def test_setup_watcher_model_cache_preserves_decimal_catalog_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models.json"
    _write_catalog(path, ("one",))
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '"input": 1,',
            '"input": 0.12345678901234567890123456789,',
        ),
        encoding="utf-8",
    )
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    expected = asyncio.run(watcher.refresh())
    asyncio.run(watcher.refresh())

    warm = SetupWatcher(AgentLayout.resident(tmp_path, "alice"))
    actual = asyncio.run(warm.refresh())

    expected_cost = expected.providers["test"].models["one"].cost
    actual_cost = actual.providers["test"].models["one"].cost
    assert expected_cost is not None
    assert actual_cost is not None
    assert actual_cost["input"] == expected_cost["input"]
    assert isinstance(actual_cost["input"], Decimal)


def test_setup_watcher_retries_transient_model_cache_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    original_store = watcher._model_cache.store
    store_calls = 0

    def flaky_store(**kwargs: object) -> None:
        nonlocal store_calls
        store_calls += 1
        if store_calls == 1:
            raise OSError("temporary cache failure")
        original_store(**kwargs)

    monkeypatch.setattr(watcher._model_cache, "store", flaky_store)

    asyncio.run(watcher.refresh())
    asyncio.run(watcher.refresh())
    cache = tmp_path / ".setup" / "models" / "projection.json"
    assert not cache.exists()

    asyncio.run(watcher.refresh())

    assert store_calls == 2
    assert cache.is_file()


def test_catalog_inspection_reuses_setup_model_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one", "two"))
    envs = {"TEST_API_KEY": "secret"}
    watcher = _watcher(monkeypatch, tmp_path, envs=envs)
    asyncio.run(watcher.refresh())
    asyncio.run(watcher.refresh())

    async def reject_static(_self: object) -> ModelCatalogSnapshot:
        raise AssertionError("inspection must not reread the source catalog")

    def reject_projection(_model: Model) -> ModelInfo:
        raise AssertionError("inspection must reuse the derived projection")

    adapters = {
        "chat_completions": ChatCompletionsModelAdapter(),
        "responses": ResponsesModelAdapter(),
    }
    monkeypatch.setattr(ModelsDevModelCatalog, "snapshot", reject_static)
    monkeypatch.setattr(catalog_module, "load_setup_config", lambda _layout: {})
    monkeypatch.setattr(catalog_module, "load_agent_config", lambda _layout: {})
    monkeypatch.setattr(catalog_module, "load_setup_envs", lambda _layout: envs)
    monkeypatch.setattr(catalog_module, "load_model_adapters", lambda _config: adapters)
    monkeypatch.setattr(catalog_module, "model_info_from_catalog", reject_projection)

    inspection = asyncio.run(
        load_catalog_inspection(AgentLayout.resident(tmp_path, "alice"))
    )

    assert inspection.models.refs() == ("test/one", "test/two")


@pytest.mark.parametrize(
    "cache_content",
    (
        "not json",
        '{"schema": 999}',
    ),
)
def test_setup_watcher_treats_invalid_model_cache_as_a_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cache_content: str,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    asyncio.run(watcher.refresh())
    asyncio.run(watcher.refresh())
    cache = tmp_path / ".setup" / "models" / "projection.json"
    cache.write_text(cache_content, encoding="utf-8")
    parse_calls = 0
    original_snapshot = ModelsDevModelCatalog.snapshot

    async def count_parse(self: ModelsDevModelCatalog) -> ModelCatalogSnapshot:
        nonlocal parse_calls
        parse_calls += 1
        return await original_snapshot(self)

    monkeypatch.setattr(ModelsDevModelCatalog, "snapshot", count_parse)
    fresh = SetupWatcher(AgentLayout.resident(tmp_path, "alice"))

    asyncio.run(fresh.refresh())

    assert parse_calls == 1


def test_setup_watcher_treats_stale_model_cache_as_a_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models.json"
    _write_catalog(path, ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    asyncio.run(watcher.refresh())
    asyncio.run(watcher.refresh())
    _write_catalog(path, ("one", "two"))
    parse_calls = 0
    original_snapshot = ModelsDevModelCatalog.snapshot

    async def count_parse(self: ModelsDevModelCatalog) -> ModelCatalogSnapshot:
        nonlocal parse_calls
        parse_calls += 1
        return await original_snapshot(self)

    monkeypatch.setattr(ModelsDevModelCatalog, "snapshot", count_parse)
    fresh = SetupWatcher(AgentLayout.resident(tmp_path, "alice"))

    setup = asyncio.run(fresh.refresh())

    assert parse_calls == 1
    assert setup.models.refs() == ("test/one", "test/two")


def test_model_cache_does_not_bypass_catalog_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    expected = asyncio.run(watcher.refresh())
    asyncio.run(watcher.refresh())
    config = {
        "plugin": {
            "model_catalog": {
                "models_dev": {"max_bytes": 1},
            }
        }
    }
    (tmp_path / "config.toml").write_text(
        "[plugin.model_catalog.models_dev]\nmax_bytes = 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(watcher_module, "load_setup_config", lambda _layout: config)

    assert asyncio.run(watcher.refresh()) is expected
    assert watcher.diagnostics()[0].code == "value-error"
    assert "model catalog exceeds 1 bytes" in watcher.diagnostics()[0].message

    fresh = SetupWatcher(AgentLayout.resident(tmp_path, "alice"))

    with pytest.raises(ValueError, match="model catalog exceeds 1 bytes"):
        asyncio.run(fresh.refresh())

    monkeypatch.setattr(catalog_module, "load_setup_config", lambda _layout: config)
    monkeypatch.setattr(catalog_module, "load_agent_config", lambda _layout: {})
    monkeypatch.setattr(
        catalog_module,
        "load_setup_envs",
        lambda _layout: {"TEST_API_KEY": "secret"},
    )

    with pytest.raises(ValueError, match="model catalog exceeds 1 bytes"):
        asyncio.run(load_catalog_inspection(AgentLayout.resident(tmp_path, "alice")))


def test_large_model_cache_avoids_duplicate_derived_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_ids = tuple(f"model-{index}" for index in range(513))
    _write_catalog(tmp_path / "models.json", model_ids)
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    expected = asyncio.run(watcher.refresh())
    asyncio.run(watcher.refresh())
    cache = tmp_path / ".setup" / "models" / "projection.json"

    payload = json.loads(cache.read_text(encoding="utf-8"))

    assert payload["projection"]["models"] is None

    async def reject_static(_self: object) -> ModelCatalogSnapshot:
        raise AssertionError("large warm refresh must reuse the normalized catalog")

    projection_calls = 0
    original_model_info = watcher_module.model_info_from_catalog

    def count_projection(model: Model) -> ModelInfo:
        nonlocal projection_calls
        projection_calls += 1
        return original_model_info(model)

    monkeypatch.setattr(ModelsDevModelCatalog, "snapshot", reject_static)
    monkeypatch.setattr(watcher_module, "model_info_from_catalog", count_projection)
    warm = SetupWatcher(AgentLayout.resident(tmp_path, "alice"))

    setup = asyncio.run(warm.refresh())

    assert projection_calls == len(model_ids)
    assert len(setup.models.entries) == len(model_ids)
    assert setup.models.entries[0].info.input_price == (
        expected.models.entries[0].info.input_price
    )


def test_model_cache_rejects_catalog_headers_that_can_contain_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models.json"
    _write_catalog(path, ("one",))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["test"]["models"]["one"]["experimental"] = {
        "modes": {
            "private": {
                "provider": {
                    "headers": {"Authorization": "secret"},
                }
            }
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})

    asyncio.run(watcher.refresh())
    asyncio.run(watcher.refresh())

    assert not (tmp_path / ".setup" / "models" / "projection.json").exists()


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
    assert second.models.contains("ollama/new-local")


def test_setup_watcher_serializes_refreshes_and_probes_catalogs_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one",))
    active = 0
    maximum = 0

    async def observed_snapshot(self: object) -> ModelCatalogSnapshot:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.001)
        active -= 1
        provider_id = (
            "ollama" if self.__class__.__name__ == "OllamaModelCatalog" else "llama_cpp"
        )
        return _empty_local(provider_id)

    monkeypatch.setattr(OllamaModelCatalog, "snapshot", observed_snapshot)
    monkeypatch.setattr(LlamaCppModelCatalog, "snapshot", observed_snapshot)
    watcher = _watcher(
        monkeypatch,
        tmp_path,
        envs={"TEST_API_KEY": "secret"},
        patch_local=False,
    )

    async def refresh_together() -> tuple[object, ...]:
        return tuple(await asyncio.gather(*(watcher.refresh() for _ in range(3))))

    setups = asyncio.run(refresh_together())

    assert maximum == 2
    assert setups[0] is setups[1] is setups[2]


def test_setup_watcher_retains_last_setup_when_catalog_probe_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "models.json", ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    expected = asyncio.run(watcher.refresh())

    async def reject_probe(_self: object) -> ModelCatalogSnapshot:
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(OllamaModelCatalog, "snapshot", reject_probe)

    rejected = asyncio.run(watcher.refresh())

    assert rejected is expected
    assert watcher.diagnostics()[0].code == "runtime-error"
    assert watcher.diagnostics()[0].message == "catalog unavailable"

    async def recovered(_self: object) -> ModelCatalogSnapshot:
        return _empty_local("ollama")

    monkeypatch.setattr(OllamaModelCatalog, "snapshot", recovered)

    assert asyncio.run(watcher.refresh()) is expected
    assert watcher.diagnostics() == ()


def test_setup_watcher_uses_five_second_default_probe_interval() -> None:
    assert DEFAULT_INTERVAL_MS == 5_000.0


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
    (tmp_path / ".env").write_text("TEST_API_KEY=second\n", encoding="utf-8")
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

    actual = asyncio.run(watcher.refresh())

    assert actual is expected
    assert watcher.current() is expected
    assert watcher.diagnostics()[0].code == "value-error"
    assert "invalid model catalog JSON" in watcher.diagnostics()[0].message


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
    assert len(setup.models._matcher.items) == 1
    assert len(setup.tools._matcher.items) == 1
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

    refreshed = asyncio.run(watcher.refresh())

    assert refreshed is initial
    assert watcher.current() is initial
    assert watcher.diagnostics()[0].code == "toolang-error"


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
