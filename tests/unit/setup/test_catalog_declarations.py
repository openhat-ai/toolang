"""Portable plugin declarations become host records without changing facts."""

import asyncio
from dataclasses import replace

import pytest

from toolang.base.types.model import CatalogModel, CatalogProvider, CatalogSnapshot
from toolang.plugin.adapters.chat_completions import ChatCompletionsModelAdapter
from toolang.plugin.models.budget import output_budget, input_budget
from toolang.setup.catalog import MergedModelCatalog, assemble_catalog
from toolang.setup.cache import ModelCatalogCache
from toolang.setup.routes import resolve_catalog_providers


class ThirdPartyCatalog:
    name = "third_party"

    async def snapshot(self):
        return CatalogSnapshot(
            providers={
                "custom": CatalogProvider(
                    "custom",
                    "Custom",
                    api="https://example.test/v1",
                    adapter="chat_completions",
                )
            },
            models=(CatalogModel("m", "Model", provider_id="custom", reasoning=True),),
            revision="1",
        )


def test_minimal_neutral_catalog_is_assembled_routed_and_cached(tmp_path):
    raw = asyncio.run(ThirdPartyCatalog().snapshot())
    snapshot = asyncio.run(MergedModelCatalog((ThirdPartyCatalog(),)).snapshot())
    routed = resolve_catalog_providers(
        snapshot,
        adapters={"chat_completions": ChatCompletionsModelAdapter()},
        environ={},
    )
    model = routed.models[0]
    assert model._toolang.ready
    assert model.to_data() == raw.models[0].to_data()
    assert output_budget(model.limit) == 32768
    assert input_budget(model.limit, 32768) is None
    cache = ModelCatalogCache(tmp_path)
    cache.store_source("custom", revision="1", snapshot=routed)
    restored = cache.load_source("custom", revision="1")
    assert restored is not None
    assert restored.models[0].limit == {}
    assert restored.models[0].reasoning_options is None
    assert not restored.models[0]._toolang.ready


@pytest.mark.parametrize("provider", ["custom", "ollama", "llama_cpp", "cloud"])
def test_budget_is_invariant_to_catalog_origin(provider):
    raw = CatalogSnapshot(
        providers={provider: CatalogProvider(provider, provider)},
        models=(
            CatalogModel("m", "M", provider_id=provider, limit={"context": 32768}),
        ),
        revision="1",
    )
    model = assemble_catalog(raw).models[0]
    assert output_budget(model.limit) == 8192
    assert input_budget(model.limit, 8192) == 22937
    assert model.limit == {"context": 32768}


def test_refresh_replaces_endpoint_facts_without_mutating_previous_snapshot():
    raw = asyncio.run(ThirdPartyCatalog().snapshot())
    first = assemble_catalog(
        replace(
            raw,
            models=(replace(raw.models[0], limit={"context": 32768, "output": 8192}),),
        )
    )
    second = assemble_catalog(
        replace(
            raw,
            providers={
                "custom": replace(
                    raw.providers["custom"], api="https://another.test/v1"
                )
            },
            revision="2",
        )
    )
    assert first.models[0].limit == {"context": 32768, "output": 8192}
    assert second.models[0].limit == {}
    assert second.providers["custom"].api == "https://another.test/v1"


def test_old_catalog_cache_is_invalidated(tmp_path):
    from toolang.common.cache import store_document
    from toolang.setup.cache import _snapshot_document

    snapshot = assemble_catalog(asyncio.run(ThirdPartyCatalog().snapshot()))
    store_document(
        tmp_path / "custom.json",
        kind="catalog",
        key="custom",
        document={**_snapshot_document(snapshot), "revision": "1"},
    )
    assert ModelCatalogCache(tmp_path).load_source("custom", revision="1") is None
