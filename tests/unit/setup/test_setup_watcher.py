from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
from typing import Any

import pytest

from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.tool import ToolContext, ToolDefinition, ToolResult
from toolang.common.errors import ToolangError
from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelRequest,
    ModelToolang,
    Provider,
    Reasoning,
)
from toolang.common.layout import AgentLayout
from toolang.plugin.adapters.responses import ResponsesModelAdapter
from toolang.plugin.adapters.chat_completions import (
    ChatCompletionsModelAdapter,
)
from toolang.plugin.catalogs.models_dev.catalog import ModelCatalogSource
from toolang.plugin.catalogs.llama_cpp import LlamaCppModelCatalog
from toolang.plugin.catalogs.ollama import OllamaModelCatalog
from toolang.setup import SetupWatcher
from toolang.setup import watcher as watcher_module
from toolang.setup.watcher import DEFAULT_INTERVAL_MS, load_setup
from toolang.base.protocols.tool import Tool


class _Tool(Tool):
    name = "one"
    plugin_name = "test"
    toolset = "alpha"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description="Use alpha one.")

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        return ToolResult({})


def test_setup_watcher_current_requires_initial_refresh(tmp_path: Path) -> None:
    watcher = SetupWatcher(AgentLayout.resident(tmp_path, "alice"))

    with pytest.raises(RuntimeError, match="has not been refreshed"):
        watcher.current()


def test_setup_watcher_persists_secret_free_model_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one", "two"))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})

    setup = asyncio.run(watcher.refresh())
    asyncio.run(watcher.refresh())

    assert tuple(setup.providers) == ("test",)
    assert setup.models.refs() == ("test/one", "test/two")
    assert all(
        model._toolang.route.adapter == "responses" for model in setup.models.entries
    )
    cache_files = _context_cache_files(tmp_path, "alice")
    assert _model_cache_names(tmp_path, "alice") == (
        "llama_cpp",
        "models_dev",
        "ollama",
    )
    assert all("secret" not in path.read_text(encoding="utf-8") for path in cache_files)


def test_setup_watcher_keeps_runtime_sandbox_separate_from_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one",))
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


def test_setup_watcher_warm_process_reuses_persistent_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one", "two"))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    expected = asyncio.run(watcher.refresh())
    asyncio.run(watcher.refresh())

    warm = SetupWatcher(AgentLayout.resident(tmp_path, "alice"))

    actual = asyncio.run(warm.refresh())

    assert actual.models.refs() == expected.models.refs()


def test_setup_watcher_reuses_portable_cache_after_root_remount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_root = tmp_path / "host"
    guest_root = tmp_path / "guest"
    host_root.mkdir()
    guest_root.mkdir()
    _write_catalog(host_root / "catalog.json", ("one", "two"))
    expected = asyncio.run(
        _watcher(
            monkeypatch,
            host_root,
            envs={"TEST_API_KEY": "secret"},
        ).refresh()
    )
    assert all(
        str(host_root) not in cache.read_text(encoding="utf-8")
        for cache in (*_context_cache_files(host_root, "alice"),)
    )
    shutil.copy2(host_root / "catalog.json", guest_root / "catalog.json")
    source_home_cache = host_root / "agents" / "alice" / ".setup"
    target_home_cache = guest_root / "agents" / "alice" / ".setup"
    target_home_cache.parent.mkdir(parents=True)
    shutil.copytree(source_home_cache, target_home_cache)

    actual = asyncio.run(
        SetupWatcher(AgentLayout.resident(guest_root, "alice")).refresh()
    )

    assert actual.models.refs() == expected.models.refs()


def test_model_context_ignores_defaults_limits_and_tool_allow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one",))
    _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    layout = AgentLayout.resident(tmp_path, "alice")
    asyncio.run(SetupWatcher(layout).refresh())
    config = {
        "allow": {"tools": ["alpha/*"]},
        "default": {"model": "test/one"},
        "limit": {"tokens": 123},
    }
    monkeypatch.setattr(watcher_module, "load_setup_config", lambda _layout: config)

    setup = asyncio.run(SetupWatcher(layout).refresh())

    assert setup.defaults.model == ModelRequest("test/one")
    assert setup.limits.tokens == 123
    assert _model_cache_names(tmp_path, "alice") == (
        "llama_cpp",
        "models_dev",
        "ollama",
    )


def test_model_cache_separates_root_and_agent_contexts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one",))
    envs = {"TEST_API_KEY": "secret"}
    _watcher(monkeypatch, tmp_path, envs=envs)

    alice = asyncio.run(SetupWatcher(AgentLayout.resident(tmp_path, "alice")).refresh())
    bob = asyncio.run(SetupWatcher(AgentLayout.resident(tmp_path, "bob")).refresh())

    assert alice.models.refs() == ("test/one",)
    assert bob.models.refs() == ("test/one",)
    assert _model_cache_names(tmp_path, "alice") == (
        "llama_cpp",
        "models_dev",
        "ollama",
    )
    assert _model_cache_names(tmp_path, "bob") == ("llama_cpp", "models_dev", "ollama")
    assert _context_cache_files(tmp_path, "alice") != _context_cache_files(
        tmp_path, "bob"
    )


def test_setup_watcher_model_cache_preserves_decimal_catalog_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.json"
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


def test_setup_watcher_warm_projection_preserves_empty_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.json"
    _write_catalog(path, ("one",))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["empty"] = {
        "id": "empty",
        "name": "Empty",
        "env": [],
        "npm": "@ai-sdk/openai",
        "models": {},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})

    cold = asyncio.run(watcher.refresh())
    warm = asyncio.run(SetupWatcher(AgentLayout.resident(tmp_path, "alice")).refresh())

    assert tuple(cold.providers) == ("test",)
    assert tuple(warm.providers) == ("test",)


@pytest.mark.parametrize(
    "cache_content",
    (
        "not json",
        '{"digest":"sha256:0","payload":{}}',
    ),
)
def test_setup_watcher_treats_invalid_context_cache_as_a_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cache_content: str,
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    asyncio.run(watcher.refresh())
    cache = _context_cache_files(tmp_path, "alice")[0]
    cache.write_text(cache_content, encoding="utf-8")

    fresh = SetupWatcher(AgentLayout.resident(tmp_path, "alice"))

    asyncio.run(fresh.refresh())


def test_setup_watcher_ignores_legacy_models_context_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    asyncio.run(watcher.refresh())
    effective = _context_cache_files(tmp_path, "alice")[0]
    legacy = effective.with_name("models.json")
    effective.replace(legacy)

    fresh = SetupWatcher(AgentLayout.resident(tmp_path, "alice"))

    asyncio.run(fresh.refresh())

    assert effective.is_file()
    assert legacy.is_file()


def test_setup_watcher_treats_stale_model_cache_as_a_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.json"
    _write_catalog(path, ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    asyncio.run(watcher.refresh())
    asyncio.run(watcher.refresh())
    _write_catalog(path, ("one", "two"))
    parse_calls = 0
    original_snapshot = ModelCatalogSource.snapshot

    def count_parse(self: ModelCatalogSource) -> ModelCatalogSnapshot:
        nonlocal parse_calls
        parse_calls += 1
        return original_snapshot(self)

    monkeypatch.setattr(ModelCatalogSource, "snapshot", count_parse)
    fresh = SetupWatcher(AgentLayout.resident(tmp_path, "alice"))

    setup = asyncio.run(fresh.refresh())

    assert parse_calls == 1
    assert setup.models.refs() == ("test/one", "test/two")


def test_model_cache_does_not_bypass_catalog_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one",))
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

    monkeypatch.setattr(watcher_module, "load_setup_config", lambda _layout: config)
    monkeypatch.setattr(watcher_module, "load_agent_config", lambda _layout: {})
    monkeypatch.setattr(
        watcher_module,
        "load_setup_envs",
        lambda _layout: {"TEST_API_KEY": "secret"},
    )

    with pytest.raises(ValueError, match="model catalog exceeds 1 bytes"):
        asyncio.run(load_setup(AgentLayout.resident(tmp_path, "alice")))


def test_model_cache_rebinds_secret_model_headers_without_persisting_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.json"
    _write_catalog(path, ("one",))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["test"]["api"] = "https://gateway.test/v1?api_key=secret"
    payload["test"]["models"]["one"]["provider"] = {"mode": "private"}
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

    setup = asyncio.run(watcher.refresh())

    context_files = _context_cache_files(tmp_path, "alice")
    assert all(
        "secret" not in cache.read_text(encoding="utf-8") for cache in context_files
    )
    route = setup.models.resolve("test/one")._toolang.route
    assert route.headers == {"Authorization": "secret"}
    assert route.api == "https://gateway.test/v1?api_key=secret"

    warm = SetupWatcher(AgentLayout.resident(tmp_path, "alice"))

    warm_setup = asyncio.run(warm.refresh())
    warm_route = warm_setup.models.resolve("test/one")._toolang.route
    assert warm_route.headers == {"Authorization": "secret"}
    assert warm_route.api == "https://gateway.test/v1?api_key=secret"


def test_setup_watcher_detects_local_models_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one",))
    calls = 0

    async def changing_ollama(_self: object) -> ModelCatalogSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _empty_local("ollama")
        model = Model(
            id="new-local",
            name="New Local",
            _toolang=ModelToolang(provider="ollama", ready=True),
        )
        provider = Provider(
            id="ollama",
            name="Ollama",
            env=(),
            npm="@ai-sdk/openai-compatible",
            api="http://127.0.0.1:11434/v1",
            models={model.id: model},
        )
        return ModelCatalogSnapshot(
            providers={provider.id: provider},
            models=(model,),
            revision=f"runtime:ollama:{calls}",
            local=True,
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
    _write_catalog(tmp_path / "catalog.json", ("one",))
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
    _write_catalog(tmp_path / "catalog.json", ("one",))
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
    path = tmp_path / "catalog.json"
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
    _write_catalog(tmp_path / "catalog.json", ("one",))
    envs = {"TEST_API_KEY": "first"}
    watcher = _watcher(monkeypatch, tmp_path, envs=envs)
    first = asyncio.run(watcher.refresh())

    envs["TEST_API_KEY"] = "second"
    (tmp_path / ".env").write_text("TEST_API_KEY=second\n", encoding="utf-8")
    second = asyncio.run(watcher.refresh())

    assert first is not second
    assert second.envs["TEST_API_KEY"] == "second"


@pytest.mark.parametrize("agent_context", [True, False])
def test_setup_watcher_publishes_tool_environment_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_context: bool,
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one",))
    envs = {"TEST_API_KEY": "secret", "SERVICE_TOKEN": "first"}
    _watcher(monkeypatch, tmp_path, envs=envs)
    monkeypatch.setattr(
        watcher_module, "load_root_setup_envs", lambda _layout: dict(envs)
    )
    watcher = SetupWatcher(
        AgentLayout.resident(tmp_path, "alice"), agent_context=agent_context
    )
    first = asyncio.run(watcher.refresh())

    envs["SERVICE_TOKEN"] = "second"
    watcher.layout.root_env.write_text("# changed\n", encoding="utf-8")
    second = asyncio.run(watcher.refresh())

    assert second is not first
    assert second.revision != first.revision
    assert first.envs["SERVICE_TOKEN"] == "first"
    assert second.envs["SERVICE_TOKEN"] == "second"
    assert asyncio.run(watcher.refresh()) is second


def test_setup_watcher_queries_effective_adapters_before_applying_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "catalog.json"
    _write_catalog(path, ("one", "two"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["test"]["models"]["two"]["provider"] = {
        "shape": "chat_completions",
        "api": "https://models.example/v1",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})

    setup = asyncio.run(watcher.refresh())

    assert setup.models.match("*[adapter=responses]").refs() == ("test/one",)
    assert setup.models.match("*[route.adapter=chat_completions]").refs() == (
        "test/two",
    )
    allowed = asyncio.run(
        SetupWatcher(
            watcher.layout, allow_overrides={"models": ("*[adapter=responses]",)}
        ).refresh()
    )
    assert allowed.models.refs() == ("test/one",)


def test_setup_watcher_warm_cache_avoids_source_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    first = asyncio.run(watcher.refresh())

    def reject_parse(_self: ModelCatalogSource) -> ModelCatalogSnapshot:
        raise AssertionError("an unchanged cached catalog must not be parsed again")

    monkeypatch.setattr(ModelCatalogSource, "snapshot", reject_parse)
    warm = SetupWatcher(watcher.layout)
    loaded = asyncio.run(warm.refresh())

    assert loaded.models == first.models
    assert loaded.revision == first.revision
    assert asyncio.run(warm.refresh()) is loaded
    assert warm.diagnostics() == ()


def test_setup_watcher_keeps_probe_changes_when_cache_write_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    first = asyncio.run(watcher.refresh())
    model = Model(
        id="new-local", name="Local", _toolang=ModelToolang(provider="ollama")
    )
    provider = Provider(
        id="ollama",
        name="Ollama",
        npm="@ai-sdk/openai-compatible",
        api="http://localhost/v1?api_key=test-placeholder",
        models={model.id: model},
    )
    probe = replace(
        _empty_local("ollama"), providers={provider.id: provider}, models=(model,)
    )

    async def changed_probe(_self: object) -> ModelCatalogSnapshot:
        return probe

    monkeypatch.setattr(OllamaModelCatalog, "snapshot", changed_probe)
    second = asyncio.run(watcher.refresh())

    assert second is not first
    assert second.models.contains("ollama/new-local")
    assert asyncio.run(watcher.refresh()) is second
    assert watcher.diagnostics() == ()


def test_setup_watcher_failed_refresh_keeps_last_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.json"
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
    _write_catalog(tmp_path / "catalog.json", ("one",))
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
    _write_catalog(tmp_path / "catalog.json", ("one", "two"))
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
    assert setup.defaults.model == ModelRequest("test/one")
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
    _write_catalog(tmp_path / "catalog.json", ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})

    setup = asyncio.run(watcher.refresh())

    assert setup.defaults.model is None


@pytest.mark.parametrize("query", [None, "aardvark/*, openai/*, *"])
def test_setup_model_order_survives_cached_publication(tmp_path, monkeypatch, query):
    path = tmp_path / "catalog.json"
    _write_catalog(path, ("z", "a"))
    provider = json.loads(path.read_text())["test"]
    path.write_text(
        json.dumps(
            {
                name: {**provider, "id": name, "api": "https://example.test/v1"}
                for name in ("aardvark", "openai", "google")
            }
        )
    )
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    config = {} if query is None else {"allow": {"models": query}}
    monkeypatch.setattr(watcher_module, "load_setup_config", lambda _layout: config)
    first = asyncio.run(watcher.refresh())
    providers = (
        ("google", "openai", "aardvark")
        if query is None
        else ("aardvark", "openai", "google")
    )
    expected = tuple(
        f"{provider}/{model}" for provider in providers for model in ("a", "z")
    )
    assert first.models.refs() == expected
    warm = asyncio.run(SetupWatcher(watcher.layout).refresh())
    assert warm.models.refs() == expected


def test_setup_watcher_rejects_compact_excluded_from_effective_models(
    tmp_path, monkeypatch
):
    _write_catalog(tmp_path / "catalog.json", ("one", "two"))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    config = {"allow": {"models": "test/one"}, "compact": {"model": "test/two"}}
    monkeypatch.setattr(watcher_module, "load_setup_config", lambda _layout: config)
    with pytest.raises(ToolangError, match="available, allowed"):
        asyncio.run(watcher.refresh())


def test_setup_watcher_rejects_default_excluded_from_effective_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one", "two"))
    config = {
        "allow": {"models": ["test/one"]},
        "default": {"model": "test/two"},
    }
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    monkeypatch.setattr(watcher_module, "load_setup_config", lambda _layout: config)

    with pytest.raises(ToolangError, match="model ref is unavailable: test/two"):
        asyncio.run(watcher.refresh())


def test_setup_watcher_validates_default_model_parameters_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(
        tmp_path / "catalog.json",
        ("one",),
        reasoning=True,
        exhaustive_reasoning=True,
    )
    config: dict[str, object] = {"default": {"model": "test/one effort=high"}}
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    monkeypatch.setattr(watcher_module, "load_setup_config", lambda _layout: config)

    setup = asyncio.run(watcher.refresh())

    assert setup.defaults.model == ModelRequest(
        "test/one",
        reasoning=Reasoning(effort="high"),
    )

    config["default"] = {"model": "test/one effort=max"}
    rejected = asyncio.run(watcher.refresh())

    assert rejected is setup
    assert (
        "does not advertise reasoning effort 'max'" in watcher.diagnostics()[0].message
    )


def test_setup_watcher_retains_last_setup_when_default_becomes_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.json"
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
    _write_catalog(tmp_path / "catalog.json", ("one",))
    config: dict[str, object] = {"allow": {"prompts": ["prompt/one"]}}
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    monkeypatch.setattr(watcher_module, "load_setup_config", lambda _layout: config)
    initial = asyncio.run(watcher.refresh())
    config = {"allow": {"prompts": ["prompt/two"]}}

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


def test_tool_allow_filters_user_tools_but_keeps_runtime_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from toolang.plugin.toolsets.loading import load_tools

    registered = load_tools()
    _write_catalog(tmp_path / "catalog.json", ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={})
    monkeypatch.setattr(watcher_module, "load_tools", lambda **kwargs: registered)
    monkeypatch.setattr(
        watcher_module, "load_agent_config", lambda layout: {"allow": {"tools": []}}
    )
    setup = asyncio.run(watcher.refresh())
    assert not setup.tools.user
    assert set(setup.tools) == {
        "_toolang__run",
        "_toolang__execute",
        "_toolang__pick",
        "_toolang__honor",
        "_toolang__reload",
        "_toolang__compact",
    }


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
    )
    return ModelCatalogSnapshot(
        providers={provider_id: provider},
        models=(),
        revision=f"runtime:{provider_id}",
        local=True,
    )


def _write_catalog(
    path: Path,
    model_ids: tuple[str, ...],
    *,
    reasoning: bool = False,
    exhaustive_reasoning: bool = False,
) -> None:
    models = {
        model_id: {
            "id": model_id,
            "name": model_id.title(),
            "attachment": False,
            "reasoning": reasoning,
            "reasoning_options": (
                [
                    {
                        "type": "effort",
                        "values": ["low", "high"],
                        "exhaustive": exhaustive_reasoning,
                    }
                ]
                if reasoning
                else None
            ),
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


def _context_cache_files(root: Path, agent: str) -> tuple[Path, ...]:
    return tuple(sorted((root / "agents" / agent / ".setup" / "models").glob("*.json")))


def _model_cache_names(root: Path, agent: str) -> tuple[str, ...]:
    return tuple(path.stem for path in _context_cache_files(root, agent))


def _root_context_cache_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted((root / ".setup" / "models").glob("*.json")))


def test_local_probe_keeps_its_stamp_across_identical_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})

    first = asyncio.run(watcher.refresh())
    ollama_file = next(
        path
        for path in _context_cache_files(tmp_path, "alice")
        if path.stem == "ollama"
    )
    stamp = ollama_file.stat().st_mtime_ns
    second = asyncio.run(watcher.refresh())

    assert ollama_file.stat().st_mtime_ns == stamp
    assert second is first


def test_models_dev_revision_advances_when_the_file_is_touched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.json"
    _write_catalog(path, ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})

    first = asyncio.run(watcher.refresh())
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    second = asyncio.run(watcher.refresh())

    assert second is not first
    assert second.revision != first.revision
    assert second.models.refs() == first.models.refs()


def test_each_catalog_persists_one_file_without_unmodelled_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path / "catalog.json", ("one",))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})

    asyncio.run(watcher.refresh())

    assert _model_cache_names(tmp_path, "alice") == (
        "llama_cpp",
        "models_dev",
        "ollama",
    )
    for path in _context_cache_files(tmp_path, "alice"):
        assert "extra" not in path.read_text(encoding="utf-8")


def test_setup_filters_readiness_and_allow_but_retains_complete_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "catalog.json"
    _write_catalog(path, ("one", "two"))
    data = json.loads(path.read_text())
    data["offline"] = {**data["test"], "id": "offline", "env": ["MISSING_KEY"]}
    path.write_text(json.dumps(data))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})
    monkeypatch.setattr(
        watcher_module,
        "load_setup_config",
        lambda _layout: {"allow": {"models": "*/one"}},
    )

    setup = asyncio.run(watcher.refresh())

    assert setup.models.refs() == ("test/one",)
    assert len(setup.models._matcher.items) == 1
    assert tuple(setup.providers) == ("test",)
    assert tuple(setup.providers["test"].models) == ("one",)
    assert setup.model_catalog().models == setup.models.entries
    complete = setup.model_catalog(all=True)
    assert complete.revision == setup.revision
    assert {model.ref for model in complete.models} == {
        "test/one",
        "test/two",
        "offline/one",
        "offline/two",
    }
    assert set(complete.providers) == {"test", "offline", "ollama", "llama_cpp"}
    excluded = complete.find("test", "two")
    unready = complete.find("offline", "one")
    assert excluded is not None and excluded._toolang.ready
    assert unready is not None and not unready._toolang.ready
    assert setup.models.refs() == ("test/one",)
    persisted = json.loads(
        next(
            path
            for path in _context_cache_files(tmp_path, "alice")
            if path.stem == "models_dev"
        ).read_text()
    )
    assert len(persisted["payload"]["models"]) == 4


def test_complete_catalog_is_pinned_without_rereading_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "catalog.json"
    _write_catalog(path, ("one", "two"))
    envs = {"TEST_API_KEY": "secret"}
    watcher = _watcher(monkeypatch, tmp_path, envs=envs)
    first = asyncio.run(watcher.refresh())
    _write_catalog(path, ("three",))
    envs.clear()
    (tmp_path / ".env").write_text("# reload environment\n")
    second = asyncio.run(watcher.refresh())
    assert second.models.refs() == ()
    assert not second.providers
    path.unlink()
    shutil.rmtree(tmp_path / "agents" / "alice" / ".setup")

    old_catalog = first.model_catalog(all=True)
    new_catalog = second.model_catalog(all=True)

    assert tuple(model.ref for model in old_catalog.models) == ("test/one", "test/two")
    assert all(model._toolang.ready for model in old_catalog.models)
    assert tuple(model.ref for model in new_catalog.models) == ("test/three",)
    assert not new_catalog.models[0]._toolang.ready
    assert old_catalog.revision == first.revision != second.revision
    assert new_catalog.revision == second.revision
    assert first.catalog_sources["test"][0] == "models_dev"
    assert first.catalog_sources["test"][1] != second.catalog_sources["test"][1]


def test_automatic_compaction_ignores_unready_models(tmp_path, monkeypatch):
    from toolang.setup.models import select_compact_model

    path = tmp_path / "catalog.json"
    _write_catalog(path, ("one",))
    data = json.loads(path.read_text())
    data["anthropic"] = {**data["test"], "id": "anthropic", "env": ["MISSING_KEY"]}
    path.write_text(json.dumps(data))
    watcher = _watcher(monkeypatch, tmp_path, envs={"TEST_API_KEY": "secret"})

    setup = asyncio.run(watcher.refresh())

    assert setup.models.effective_default(None) == "test/one"
    assert select_compact_model(setup.models, None).ref == "test/one"


def test_route_environment_refresh_leaves_source_cache_and_old_views_unchanged(
    tmp_path,
    monkeypatch,
):
    _write_catalog(tmp_path / "catalog.json", ("one",))
    envs = {}
    watcher = _watcher(monkeypatch, tmp_path, envs=envs)
    first = asyncio.run(watcher.refresh())
    source_files = _context_cache_files(tmp_path, "alice")
    original_files = {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in source_files
    }
    first_full = first.model_catalog(all=True)
    assert first.models.refs() == ()
    assert first_full.models[0]._toolang.route.env is None

    envs["TEST_API_KEY"] = "secret"
    (tmp_path / ".env").write_text("# reload environment\n")
    second = asyncio.run(watcher.refresh())
    assert second.revision != first.revision
    assert second.catalog_sources == first.catalog_sources
    assert second.models.refs() == ("test/one",)
    assert second.models.entries[0]._toolang.route.env == ("TEST_API_KEY",)
    assert original_files == {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in source_files
    }
    for path in source_files:
        path.unlink()
    assert first.model_catalog(all=True) == first_full
    assert first.models.refs() == ()
    assert second.model_catalog(all=True).models == second.models.entries
