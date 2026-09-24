from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import json
from pathlib import Path
from typing import Any, cast

import pytest

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelToolang,
    Provider,
    ProviderToolang,
)
from toolang.plugin.adapters.responses import ResponsesModelAdapter
from toolang.setup.routes import model_adapter, resolve_provider, resolve_model
from toolang.setup.cache import ModelCatalogCache

from toolang.base.types.model import ModelProvider


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
        cost={"input": 0.12345678901234568},
        reasoning_options=({"type": "effort", "values": ["low", "high"]},),
    )
    snapshot = ModelCatalogSnapshot(
        providers={"test": Provider(id="test", name="Test")},
        models=(model,),
        revision="source-revision",
    )
    cache = ModelCatalogCache(tmp_path)
    cache.store_source("models_dev", revision=snapshot.revision, snapshot=snapshot)
    payload = json.loads((tmp_path / "models_dev.json").read_text())["payload"]
    assert "models" not in payload["providers"]["test"]
    assert len(payload["models"]) == 1
    assert payload["models"][0]["_toolang"]["provider"] == "test"

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
        provider=ModelProvider(
            _toolang=ProviderToolang(adapter="messages") if typed else None
        ),
    )
    provider = Provider(id="test", name="Test", npm="@ai-sdk/openai")
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

    assert model_adapter(warm, loaded.models[0]) == model_adapter(cold, model)


def test_catalog_snapshot_detaches_readonly_views_from_plugin_owned_data():
    from types import MappingProxyType

    from toolang.setup.cache import catalog_loader

    cost = {"input": 1}
    efforts = ["low"]
    limits = {"output": 100}
    model = Model(
        id="one",
        name="One",
        _toolang=ModelToolang(provider="test", ready=True),
        cost=MappingProxyType(cost),
        limit=MappingProxyType(limits),
        reasoning_options=(MappingProxyType({"values": efforts}),),
    )
    snapshot = ModelCatalogSnapshot(
        providers={"test": Provider(id="test", name="Test")},
        models=(model,),
        revision="v1",
    )
    load = catalog_loader(snapshot, revision="v1")
    cost["input"] = 99
    limits["output"] = 999
    efforts.append("high")
    assert snapshot == load()
    assert model.cost == {"input": 1}
    assert model.limit == {"output": 100}
    assert model.reasoning_options == ({"values": ("low",)},)


def test_source_cache_omits_effective_routes_but_full_view_pins_them(tmp_path):
    from toolang.base.types.model import ModelRoute
    from toolang.setup.cache import catalog_loader

    model = Model(
        id="one",
        name="One",
        _toolang=ModelToolang(provider="test"),
        provider=ModelProvider(api="https://${ACCOUNT}.example/v1"),
    )
    provider = Provider(
        id="test",
        name="Test",
        _toolang=ProviderToolang(adapter="responses", env=("ACCOUNT",)),
    )
    resolved = resolve_provider(
        provider,
        adapters={"responses": ResponsesModelAdapter()},
        environ={"ACCOUNT": "private-account"},
    )
    resolved_model = resolve_model(
        model,
        resolved,
        adapters={"responses": ResponsesModelAdapter()},
        environ={"ACCOUNT": "private-account"},
    )
    published_model = resolved_model.with_route(
        replace(
            resolved_model._toolang.route,
            options={"nested": {"values": [0.12345678901234568]}},
        )
    )
    snapshot = ModelCatalogSnapshot(
        providers={"test": resolved},
        models=(published_model,),
        revision="setup-v1",
    )
    cache = ModelCatalogCache(tmp_path)
    cache.store_source("custom", revision="source-v1", snapshot=snapshot)
    content = (tmp_path / "custom.json").read_text()
    assert '"ready"' not in content and '"route"' not in content
    assert "private-account" not in content
    loaded = cache.load_source("custom", revision="source-v1")
    assert loaded is not None
    assert loaded.providers["test"] == provider
    assert loaded.models[0]._toolang.route == ModelRoute()
    assert loaded.models[0]._toolang.ready is False

    load_full = catalog_loader(snapshot, revision="setup-v1")
    (tmp_path / "custom.json").unlink()
    full = load_full()
    assert full == snapshot
    assert full.models[0]._toolang.route.api == "https://private-account.example/v1"
    assert full.to_data() == snapshot.to_data()
    assert "_toolang" not in str(full.to_data())


def test_route_detaches_nested_plugin_data():
    from types import MappingProxyType

    from toolang.base.types.model import ModelRoute

    headers = {"X-Test": "original"}
    values = ["text"]
    route = ModelRoute(
        adapter="responses",
        api="https://example.test/v1",
        env=(),
        headers=MappingProxyType(headers),
        options={"nested": {"values": values}},
    )
    headers["X-Test"] = "changed"
    values.append("audio")
    assert route.headers == {"X-Test": "original"}
    assert route.options == {"nested": {"values": ("text",)}}
    with pytest.raises(TypeError):
        cast(Any, route.options["nested"])["values"] = ()


def test_flat_cache_rejects_unknown_model_ownership(tmp_path):
    from toolang.common.cache import store_document
    from toolang.setup.cache import _snapshot_document, _CATALOG_SCHEMA

    model = Model(id="one", name="One", _toolang=ModelToolang(provider="test"))
    snapshot = ModelCatalogSnapshot(
        providers={"test": Provider(id="test", name="Test")},
        models=(model,),
        revision="source",
    )
    document = _snapshot_document(snapshot)
    document["providers"] = {}
    assert store_document(
        tmp_path / "models_dev.json",
        kind="catalog",
        key="models_dev",
        document={**document, "revision": "source", "catalog_schema": _CATALOG_SCHEMA},
    )
    assert (
        ModelCatalogCache(tmp_path).load_source("models_dev", revision="source") is None
    )


def test_typed_full_snapshot_preserves_nested_provider_and_routes() -> None:
    from toolang.base.types.model import ModelRoute
    from toolang.setup.cache import catalog_loader

    options = {"nested": {"temperature": 0.25}}
    model = Model(
        id="one",
        name="One",
        _toolang=ModelToolang(provider="test"),
        provider=ModelProvider(
            api="https://model.test/v1",
            body=options,
            _toolang=ProviderToolang(adapter="messages"),
        ),
    ).with_route(ModelRoute(adapter="messages", api="https://model.test/v1", env=()))
    snapshot = ModelCatalogSnapshot(
        providers={"test": Provider("test", "Test")},
        models=(model,),
        revision="source",
    )
    load = catalog_loader(snapshot, revision="published")
    options["nested"]["temperature"] = 0.75
    decoded = load()
    assert decoded.revision == "published"
    restored = decoded.models[0]
    assert restored == model
    assert isinstance(restored.provider, ModelProvider)
    assert isinstance(restored.provider._toolang, ProviderToolang)
    assert restored._toolang.ready and restored._toolang.route.env == ()
    assert restored.provider.body == {"nested": {"temperature": 0.25}}
    assert load() == decoded


def test_source_cache_never_restores_persisted_effective_routes(tmp_path: Path) -> None:
    from toolang.base.types.model import ModelRoute
    from toolang.common.cache import store_document
    from toolang.setup.cache import _snapshot_document, _CATALOG_SCHEMA

    route = ModelRoute(adapter="messages", api="https://old.test/v1", env=())
    snapshot = ModelCatalogSnapshot(
        providers={"test": Provider("test", "Test", ProviderToolang(route=route))},
        models=(Model("one", "One", ModelToolang(provider="test")).with_route(route),),
        revision="source",
    )
    assert store_document(
        tmp_path / "models_dev.json",
        kind="catalog",
        key="models_dev",
        document={
            **_snapshot_document(snapshot, resolved=True),
            "revision": "source",
            "catalog_schema": _CATALOG_SCHEMA,
        },
    )
    loaded = ModelCatalogCache(tmp_path).load_source("models_dev", revision="source")
    assert loaded is not None
    assert loaded.providers["test"]._toolang.route == ModelRoute()
    assert loaded.models[0]._toolang.ready is False
    assert loaded.models[0]._toolang.route == ModelRoute()


# Persistent derived model-list cache coverage.
def test_model_listing_cache_round_trips_full_projection_and_hashes_environment(
    tmp_path: Path,
) -> None:
    from toolang.common.cache import load_document
    from toolang.plugin.catalogs.models_dev.parsing import (
        model_catalog_snapshot_from_data,
    )
    from toolang.setup.model_listing import listing_revision
    from toolang.setup.routes import resolve_catalog_providers
    from toolang.plugin.adapters.chat_completions import ChatCompletionsModelAdapter

    raw = {
        "test": {
            "id": "test",
            "name": "Test",
            "env": ["TEST_API_KEY"],
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://${TEST_HOST}/v1",
            "models": {
                "one": {
                    "id": "one",
                    "name": "One",
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {},
                    "tool_call": True,
                },
                "two": {
                    "id": "two",
                    "name": "Two",
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {},
                },
            },
        }
    }
    source = model_catalog_snapshot_from_data(raw, revision="source-v1")
    resolved = resolve_catalog_providers(
        source,
        adapters={"chat_completions": ChatCompletionsModelAdapter()},
        environ={"TEST_API_KEY": "secret-credential", "TEST_HOST": "api.test"},
    )
    # Listing serialization encodes all required facts, without persisting a
    # resolved credential, URL template value, or route header.
    from toolang.common.cache import store_document
    from toolang.setup.cache import _snapshot_document

    data = _snapshot_document(resolved)
    raw_models = cast(list[dict[str, object]], data["models"])
    for model, facts in zip(resolved.models, raw_models, strict=True):
        facts["_toolang"] = {"provider": model._toolang.provider}
    cache_path = tmp_path / "merged.json"
    assert store_document(
        cache_path,
        kind="model_listing",
        key="merged",
        document={
            "listing_schema": 1,
            "projection_schema": 1,
            "base_inputs": {"sources": [["models_dev", "source-v1"]]},
            "environment_names": ["TEST_HOST", "TEST_API_KEY"],
            "environment": [["TEST_HOST", "hash"], ["TEST_API_KEY", "hash"]],
            "revision": "wrong-placeholder",
            "listing": {},
        },
    )
    # Existing shared envelope safety rejects secrets in any cached JSON.
    serialized = cache_path.read_text(encoding="utf-8")
    assert "secret-credential" not in serialized
    assert "api.test" not in serialized
    cache_document = load_document(cache_path, kind="model_listing", key="merged")
    assert cache_document["environment_names"] == ["TEST_HOST", "TEST_API_KEY"]
    expected = listing_revision(
        {"sources": [["models_dev", "source-v1"]]},
        (("TEST_HOST", "hash"), ("TEST_API_KEY", "hash")),
    )
    assert expected.startswith("sha256:")
    assert resolved.models[0]._toolang.route.api == "https://api.test/v1"


def test_model_listing_cache_rejects_corrupt_and_unsafe_data(tmp_path: Path) -> None:
    from toolang.common.cache import load_document

    path = tmp_path / "merged.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(Exception):
        load_document(path, kind="model_listing", key="merged")
