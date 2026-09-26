"""Portable plugin declarations become host records without changing facts."""

import asyncio
from dataclasses import replace

from toolang.base.types.model import CatalogModel, CatalogProvider, CatalogSnapshot
from toolang.plugin.adapters.chat_completions import ChatCompletionsModelAdapter
from toolang.plugin.models.budget import input_budget, output_budget
from toolang.setup.catalog import MergedModelCatalog, assemble_catalog
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


def test_minimal_catalog_is_assembled_and_routed():
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


def test_budget_is_invariant_to_catalog_origin():
    raw = CatalogSnapshot(
        providers={"custom": CatalogProvider("custom", "Custom")},
        models=(
            CatalogModel("m", "M", provider_id="custom", limit={"context": 32768}),
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
