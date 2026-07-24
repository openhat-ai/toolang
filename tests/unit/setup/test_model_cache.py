from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.setup import models as models_module
from toolang.setup.models import ModelListCache, discover_models, model_cache_dir
from toolang.setup.records import ModelListRecord


@dataclass
class _Provider:
    name: str
    models: tuple[ModelInfo, ...]
    required: tuple[str, ...] = ()
    base_url: str = "https://models.example.test/v1"
    calls: int = 0
    error: Exception | None = None

    description = None

    def required_env_vars(self) -> tuple[str, ...]:
        return self.required

    def default_base_url(self, *, environ: Mapping[str, str]) -> str | None:
        del environ
        return self.base_url

    def default_api_key_env(self) -> str | None:
        return self.required[0] if self.required else None

    def list_models(self, *, environ: Mapping[str, str]) -> tuple[ModelInfo, ...]:
        del environ
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.models

    def prepare_target(self, target: ModelTarget) -> ModelTarget:
        return target


def _model(provider: str, name: str) -> ModelInfo:
    return ModelInfo(
        ref=f"{provider}/{name}",
        provider=provider,
        name=name,
        model=name,
    )


def test_model_cache_dir_uses_shared_root_runtime_models(tmp_path: Path) -> None:
    assert model_cache_dir(tmp_path) == tmp_path / ".runtime" / "models"


def test_model_list_cache_reuses_fresh_models_and_refreshes_explicitly(
    tmp_path: Path,
) -> None:
    provider = _Provider("remote", (_model("remote", "one"),))
    cache = ModelListCache(tmp_path / "models")

    first = asyncio.run(cache.get(provider, envs={}))
    second = asyncio.run(cache.get(provider, envs={}))
    refreshed = asyncio.run(cache.get(provider, envs={}, refresh=True))
    record = cache.read(provider, envs={})

    assert first == second == refreshed
    assert provider.calls == 2
    assert record is not None
    assert record.generation == 2
    assert record.models == first


def test_remote_refresh_failure_keeps_last_good_models(tmp_path: Path) -> None:
    provider = _Provider("remote", (_model("remote", "one"),))
    cache = ModelListCache(tmp_path / "models")
    expected = asyncio.run(cache.get(provider, envs={}))
    provider.error = RuntimeError("offline")

    actual = asyncio.run(cache.get(provider, envs={}, refresh=True))
    record = cache.read(provider, envs={})

    assert actual == expected
    assert record is not None
    assert record.generation == 1


def test_local_refresh_failure_does_not_report_stale_availability(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        "local",
        (_model("local", "one"),),
        base_url="http://127.0.0.1:9000/v1",
    )
    cache = ModelListCache(tmp_path / "models")
    asyncio.run(cache.get(provider, envs={}))
    provider.error = RuntimeError("offline")

    assert asyncio.run(cache.get(provider, envs={}, refresh=True)) == ()


def test_cache_paths_and_payloads_do_not_expose_secret_values(tmp_path: Path) -> None:
    provider = _Provider(
        "remote",
        (_model("remote", "one"),),
        required=("REMOTE_API_KEY",),
    )
    cache = ModelListCache(tmp_path / "models")

    asyncio.run(cache.get(provider, envs={"REMOTE_API_KEY": "super-secret"}))

    paths = tuple((tmp_path / "models").iterdir())
    assert paths
    assert all("super-secret" not in path.name for path in paths)
    assert all(
        "super-secret" not in path.read_text(encoding="utf-8")
        for path in paths
        if path.suffix == ".json"
    )


def test_different_credentials_use_independent_cache_records(tmp_path: Path) -> None:
    provider = _Provider(
        "remote",
        (_model("remote", "one"),),
        required=("REMOTE_API_KEY",),
    )
    directory = tmp_path / "models"
    cache = ModelListCache(directory)

    asyncio.run(cache.get(provider, envs={"REMOTE_API_KEY": "first"}))
    asyncio.run(cache.get(provider, envs={"REMOTE_API_KEY": "second"}))

    assert provider.calls == 2
    assert len(tuple(directory.glob("*.json"))) == 2


def test_corrupted_cache_record_is_replaced_by_discovery(tmp_path: Path) -> None:
    provider = _Provider("remote", (_model("remote", "one"),))
    directory = tmp_path / "models"
    cache = ModelListCache(directory)
    expected = asyncio.run(cache.get(provider, envs={}))
    path = next(directory.glob("*.json"))
    path.write_text("{not-json", encoding="utf-8")

    actual = asyncio.run(cache.get(provider, envs={}))

    assert actual == expected
    assert provider.calls == 2
    assert cache.read(provider, envs={}) is not None


def test_expired_remote_cache_falls_back_to_last_good_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(models_module, "time", lambda: now)
    provider = _Provider("remote", (_model("remote", "one"),))
    cache = ModelListCache(tmp_path / "models")
    expected = asyncio.run(cache.get(provider, envs={}))
    provider.error = RuntimeError("offline")
    now += models_module.REMOTE_MODEL_LIST_TTL_SEC + 1

    actual = asyncio.run(cache.get(provider, envs={}))

    assert actual == expected
    assert provider.calls == 2


def test_successful_discovery_survives_cache_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider("remote", (_model("remote", "one"),))
    cache = ModelListCache(tmp_path / "models")

    def fail_save(_record: ModelListRecord, _path: Path) -> None:
        raise OSError("read-only cache")

    monkeypatch.setattr(ModelListRecord, "save", fail_save)

    assert asyncio.run(cache.get(provider, envs={})) == provider.models
    assert cache.read(provider, envs={}) is None


def test_discover_models_skips_unconfigured_providers_and_sorts_results(
    tmp_path: Path,
) -> None:
    configured = _Provider(
        "zeta",
        (_model("zeta", "two"), _model("zeta", "one")),
        required=("ZETA_API_KEY",),
    )
    missing = _Provider(
        "alpha",
        (_model("alpha", "hidden"),),
        required=("ALPHA_API_KEY",),
    )

    models = asyncio.run(
        discover_models(
            {"zeta": configured, "alpha": missing},
            envs={"ZETA_API_KEY": "configured"},
            cache=ModelListCache(tmp_path / "models"),
        )
    )

    assert [(model.provider, model.ref) for model in models] == [
        ("zeta", "zeta/one"),
        ("zeta", "zeta/two"),
    ]
    assert configured.calls == 1
    assert missing.calls == 0


def test_discover_models_rejects_cross_provider_entries(tmp_path: Path) -> None:
    provider = _Provider("alpha", (_model("other", "one"),))

    with pytest.raises(ValueError, match="returned model for 'other'"):
        asyncio.run(
            discover_models(
                {"alpha": provider},
                envs={},
                cache=ModelListCache(tmp_path / "models"),
            )
        )
