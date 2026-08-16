from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
import logging
from pathlib import Path
from typing import Any, cast

import pytest

from toolang.base.protocols.model import ModelAdapter
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.base.types.policy import ResourceFilter, RunBindings, RunLimits
from toolang.common.layout import AgentLayout
from toolang.plugin.models.config import ModelProviderConfig
from toolang.setup import AgentSetup, SetupWatcher
import toolang.setup.watcher as watcher_module
from toolang.setup.models import ModelListCache


@dataclass
class _Provider:
    models: tuple[ModelInfo, ...]
    name: str = "test"
    description: str | None = None
    calls: int = 0

    def required_env_vars(self) -> tuple[str, ...]:
        return ("TEST_API_KEY",)

    def default_base_url(self, *, environ: Mapping[str, str]) -> str | None:
        del environ
        return "https://models.example.test/v1"

    def default_api_key_env(self) -> str | None:
        return "TEST_API_KEY"

    def list_models(self, *, environ: Mapping[str, str]) -> tuple[ModelInfo, ...]:
        del environ
        self.calls += 1
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


def _adapter() -> ModelAdapter:
    return cast(ModelAdapter, cast(Any, object()))


def _watcher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: _Provider,
    envs: Mapping[str, str],
    *,
    adapters: Mapping[str, ModelAdapter] | None = None,
) -> SetupWatcher:
    monkeypatch.setattr(
        watcher_module,
        "load_setup_config",
        lambda _root: {},
    )
    monkeypatch.setattr(
        watcher_module,
        "load_agent_config",
        lambda _layout: {},
    )
    monkeypatch.setattr(
        watcher_module,
        "load_setup_envs",
        lambda _root: dict(envs),
    )
    monkeypatch.setattr(
        watcher_module,
        "load_model_providers",
        lambda _configs: {"test": provider},
    )
    monkeypatch.setattr(
        watcher_module,
        "load_model_adapters",
        lambda: dict(adapters) if adapters is not None else {"default": _adapter()},
    )
    monkeypatch.setattr(
        watcher_module,
        "load_runtime_tools",
        lambda *, plugin_config: {},
    )
    return SetupWatcher(AgentLayout.resident(tmp_path, "alice"))


def test_setup_watcher_current_requires_initial_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = _watcher(
        monkeypatch,
        tmp_path,
        _Provider((_model("one"),)),
        {"TEST_API_KEY": "secret"},
    )

    with pytest.raises(RuntimeError, match="has not been refreshed"):
        watcher.current()


def test_setup_watcher_refresh_captures_discovered_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider((_model("one"),))
    watcher = _watcher(
        monkeypatch,
        tmp_path,
        provider,
        {"TEST_API_KEY": "secret"},
    )
    setup = asyncio.run(watcher.refresh())

    assert setup.models == (_model("one"),)
    assert setup.envs == {"TEST_API_KEY": "secret"}


def test_setup_watcher_loads_root_scoped_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider((_model("one"),))
    adapter = _adapter()
    tool = cast(AgentTool, cast(Any, object()))
    loaded_tool_configs: list[Mapping[str, Mapping[str, object]]] = []
    monkeypatch.setattr(
        watcher_module,
        "load_setup_config",
        lambda _root: {
            "models": {
                "providers": {"test": {"endpoint": "https://models.example.test/v1"}}
            },
            "tools": {"shell": {"token_env": "TOOL_TOKEN"}},
        },
    )
    monkeypatch.setattr(
        watcher_module,
        "load_setup_envs",
        lambda _root: {
            "TEST_API_KEY": "secret",
            "TOOL_TOKEN": "tool-secret",
        },
    )

    def load_providers(
        configs: Mapping[str, ModelProviderConfig],
    ) -> dict[str, _Provider]:
        assert configs["test"].endpoint == "https://models.example.test/v1"
        return {"test": provider}

    def load_tools(
        *,
        plugin_config: Mapping[str, Mapping[str, object]],
    ) -> dict[str, AgentTool]:
        loaded_tool_configs.append(plugin_config)
        return {"shell": tool}

    monkeypatch.setattr(watcher_module, "load_model_providers", load_providers)
    monkeypatch.setattr(
        watcher_module,
        "load_model_adapters",
        lambda: {"default": adapter},
    )
    monkeypatch.setattr(watcher_module, "load_runtime_tools", load_tools)

    layout = AgentLayout.resident(tmp_path, "alice")
    watcher = SetupWatcher(layout)
    setup = asyncio.run(watcher.refresh())

    assert watcher.layout is layout
    assert setup.layout is layout
    assert setup.providers == {"test": provider}
    assert setup.adapters == {"default": adapter}
    assert setup.tools == {"shell": tool}
    assert setup.environment is not None
    assert setup.environment.sandbox == "none"
    assert setup.environment.root == layout.root
    assert loaded_tool_configs == [{"shell": {"token": "tool-secret"}}]


def test_setup_watcher_refresh_excludes_models_without_installed_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = _model("default")
    unsupported = ModelInfo(
        ref="test/unsupported",
        provider="test",
        name="unsupported",
        model="unsupported",
        adapter="unsupported",
    )
    provider = _Provider((default, unsupported))
    watcher = _watcher(
        monkeypatch,
        tmp_path,
        provider,
        {"TEST_API_KEY": "secret"},
    )
    setup = asyncio.run(watcher.refresh())

    assert setup.models == (default,)


def test_setup_watcher_refresh_reuses_internal_shared_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider((_model("one"),))

    async def refresh() -> AgentSetup:
        return await _watcher(
            monkeypatch,
            tmp_path,
            provider,
            {"TEST_API_KEY": "secret"},
        ).refresh()

    first = asyncio.run(refresh())
    second = asyncio.run(refresh())

    assert first.models == second.models == (_model("one"),)
    assert provider.calls == 1


def test_setup_watcher_observes_external_cache_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider((_model("one"),))
    envs = {"TEST_API_KEY": "secret"}
    cache = ModelListCache(AgentLayout.resident(tmp_path, "alice").model_cache)
    watcher = _watcher(monkeypatch, tmp_path, provider, envs)
    asyncio.run(watcher.refresh())
    provider.models = (_model("two"),)
    asyncio.run(cache.get(provider, envs=envs, refresh=True))

    current = asyncio.run(watcher.refresh())

    assert current.models == (_model("two"),)


def test_setup_watcher_force_refreshes_without_exposing_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider((_model("one"),))
    envs = {"TEST_API_KEY": "secret"}
    watcher = _watcher(monkeypatch, tmp_path, provider, envs)
    asyncio.run(watcher.refresh())

    refreshed = asyncio.run(watcher.refresh(force=True))

    assert refreshed.models == (_model("one"),)
    assert provider.calls == 2


def test_setup_watcher_rebuilds_snapshot_when_envs_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider((_model("one"),))
    envs = {"TEST_API_KEY": "first"}
    watcher = _watcher(monkeypatch, tmp_path, provider, envs)
    asyncio.run(watcher.refresh())
    envs["TEST_API_KEY"] = "second"

    current = asyncio.run(watcher.refresh())

    assert current.envs == {"TEST_API_KEY": "second"}


def test_setup_watcher_rebuilds_dynamic_policy_with_frozen_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider((_model("one"),))
    watcher = _watcher(
        monkeypatch,
        tmp_path,
        provider,
        {"TEST_API_KEY": "secret"},
    )
    agent_config: dict[str, object] = {
        "allow": {"models": ["test/one"], "tools": ["shell/*"]},
        "default": {"model": "test/one"},
        "limit": {"tokens": 100},
    }
    monkeypatch.setattr(
        watcher_module,
        "load_agent_config",
        lambda _layout: agent_config,
    )
    watcher = SetupWatcher(
        watcher.layout,
        resource_filter_overrides={"tools": ()},
        binding_overrides={"runnable": "agic:chat"},
        limit_overrides={"time": 60},
    )

    first = asyncio.run(watcher.refresh())
    agent_config["allow"] = {"models": []}
    agent_config["default"] = {"model": "none"}
    agent_config["limit"] = {"tokens": 200}
    second = asyncio.run(watcher.refresh())

    assert first.resource_filter == ResourceFilter(models=("test/one",), tools=())
    assert first.bindings == RunBindings(
        model="test/one",
        runnable="agic:chat",
    )
    assert first.limits == RunLimits(tokens=100, time=60)
    assert second.resource_filter == ResourceFilter(models=(), tools=())
    assert second.bindings == RunBindings(runnable="agic:chat")
    assert second.limits == RunLimits(tokens=200, time=60)


def test_setup_watcher_failed_refresh_keeps_last_valid_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider((_model("one"),))
    watcher = _watcher(
        monkeypatch,
        tmp_path,
        provider,
        {"TEST_API_KEY": "secret"},
    )
    config: dict[str, object] = {"limit": {"tokens": 100}}
    monkeypatch.setattr(
        watcher_module,
        "load_agent_config",
        lambda _layout: config,
    )
    valid = asyncio.run(watcher.refresh())
    config["limit"] = {"tokens": -1}

    with pytest.raises(ValueError, match="non-negative"):
        asyncio.run(watcher.refresh())

    assert watcher.current() is valid


def test_setup_watcher_initial_invalid_policy_is_not_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = _watcher(
        monkeypatch,
        tmp_path,
        _Provider((_model("one"),)),
        {"TEST_API_KEY": "secret"},
    )
    monkeypatch.setattr(
        watcher_module,
        "load_agent_config",
        lambda _layout: {"allow": {"models": "test/one"}},
    )

    with pytest.raises(TypeError, match="must be an array"):
        asyncio.run(watcher.refresh())
    with pytest.raises(RuntimeError, match="has not been refreshed"):
        watcher.current()


def test_setup_watcher_updates_reports_failure_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    watcher = _watcher(
        monkeypatch,
        tmp_path,
        _Provider((_model("one"),)),
        {"TEST_API_KEY": "secret"},
    )
    initial = asyncio.run(watcher.refresh())
    recovered = replace(
        initial,
        bindings=RunBindings(model="test/one"),
    )
    refreshes = 0

    async def refresh(*, force: bool = False) -> AgentSetup:
        nonlocal refreshes
        del force
        refreshes += 1
        if refreshes == 1:
            raise ValueError("invalid dynamic setup")
        watcher._setup = recovered
        return recovered

    monkeypatch.setattr(watcher, "refresh", refresh)

    async def observe() -> AgentSetup:
        stop = asyncio.Event()
        updates = watcher.updates(stop_signal=stop, interval_ms=1)
        try:
            return await asyncio.wait_for(anext(updates), timeout=1)
        finally:
            stop.set()
            await updates.aclose()

    with caplog.at_level(logging.ERROR, logger=watcher_module.__name__):
        observed = asyncio.run(observe())

    assert observed is recovered
    assert watcher.current() is recovered
    assert "setup.refresh_failed agent=alice" in caplog.text


def test_setup_watcher_publishes_changed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> tuple[AgentSetup, AgentSetup]:
        async def next_setup(watcher: SetupWatcher, stop: asyncio.Event) -> AgentSetup:
            return await anext(
                watcher.updates(
                    stop_signal=stop,
                    interval_ms=1,
                )
            )

        provider = _Provider((_model("one"),))
        envs = {"TEST_API_KEY": "first"}
        watcher = _watcher(monkeypatch, tmp_path, provider, envs)
        await watcher.refresh()
        stop_signal = asyncio.Event()
        next_update = asyncio.create_task(next_setup(watcher, stop_signal))
        envs["TEST_API_KEY"] = "second"
        updated = await asyncio.wait_for(next_update, timeout=1)
        stop_signal.set()
        return updated, watcher.current()

    updated, current = asyncio.run(exercise())

    assert updated.envs == {"TEST_API_KEY": "second"}
    assert current is updated


def test_setup_watcher_run_stops_without_a_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> AgentSetup:
        provider = _Provider((_model("one"),))
        envs = {"TEST_API_KEY": "secret"}
        watcher = _watcher(monkeypatch, tmp_path, provider, envs)
        stop_signal = asyncio.Event()
        running = asyncio.create_task(
            watcher.run(
                stop_signal=stop_signal,
                interval_ms=1,
            )
        )
        await asyncio.sleep(0.06)
        stop_signal.set()
        await asyncio.wait_for(running, timeout=1)
        return watcher.current()

    current = asyncio.run(exercise())

    assert current.models == (_model("one"),)
    assert current.envs == {"TEST_API_KEY": "secret"}
