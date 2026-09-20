from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelToolang,
    Provider,
    ProviderToolang,
)
from toolang.plugin.adapters.responses import ResponsesModelAdapter
from toolang.plugin.models.provider_resolver import model_adapter, resolve_provider
from toolang.setup.cache import ModelCatalogCache


@pytest.mark.parametrize(
    "interleaved", [None, True, False, {"field": "reasoning_content"}]
)
def test_source_cache_round_trip_preserves_catalog_facts(
    tmp_path: Path, interleaved: bool | Mapping[str, object] | None
) -> None:
    model = Model(
        id="one",
        name="One",
        _toolang=ModelToolang(provider="test"),
        interleaved=interleaved,
        cost={"input": Decimal("0.123456789012345678901")},
        reasoning_options=({"type": "effort", "values": ["low", "high"]},),
    )
    snapshot = ModelCatalogSnapshot(
        providers={"test": Provider(id="test", name="Test", models={"one": model})},
        models=(model,),
        revision="source-revision",
    )
    cache = ModelCatalogCache(tmp_path)
    cache.store_source("models_dev", revision=snapshot.revision, snapshot=snapshot)

    loaded = ModelCatalogCache(tmp_path).load_source(
        "models_dev", revision=snapshot.revision
    )
    assert loaded == snapshot
    assert cache.load_source("models_dev", revision="changed") is None
    (tmp_path / "models_dev.json").write_text("{broken", encoding="utf-8")
    assert cache.load_source("models_dev", revision=snapshot.revision) is None


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("skip_reason", ["unsafe", "oversized"])
def test_skipped_probe_write_uses_content_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    skip_reason: str,
) -> None:
    cache = ModelCatalogCache(tmp_path)
    snapshot = ModelCatalogSnapshot(providers={}, models=(), revision="probe")
    previous = cache.store_probe("local", snapshot=snapshot) if existing else None
    provider = Provider(
        id="local",
        name="Local",
        models={},
        api="http://localhost/v1?api_key=test-placeholder",
    )
    if skip_reason == "oversized":
        provider = replace(provider, api="http://localhost/v1")
        monkeypatch.setattr("toolang.common.cache._MAX_CACHE_BYTES", 1)
    changed = replace(snapshot, providers={"local": provider})

    revision = cache.store_probe("local", snapshot=changed)

    assert revision == cache.content_revision(changed)
    assert revision != previous
    assert cache.store_probe("local", snapshot=changed) == revision


@pytest.mark.parametrize("typed", [False, True])
def test_source_cache_preserves_provider_adapter_trust(
    tmp_path: Path, typed: bool
) -> None:
    model = Model(
        id="one",
        name="One",
        _toolang=ModelToolang(provider="test"),
        provider={
            "_toolang": (
                ProviderToolang(adapter="messages")
                if typed
                else {"adapter": "messages"}
            )
        },
    )
    provider = Provider(
        id="test", name="Test", npm="@ai-sdk/openai", models={"one": model}
    )
    snapshot = ModelCatalogSnapshot(
        providers={"test": provider}, models=(model,), revision="source"
    )
    cache = ModelCatalogCache(tmp_path)
    cache.store_source("models_dev", revision="source", snapshot=snapshot)
    loaded = cache.load_source("models_dev", revision="source")
    assert loaded is not None
    adapters = {"responses": ResponsesModelAdapter()}
    cold = resolve_provider(provider, adapters=adapters, environ={})
    warm = resolve_provider(loaded.providers["test"], adapters=adapters, environ={})

    assert model_adapter(warm, warm.models["one"]) == model_adapter(
        cold, cold.models["one"]
    )
