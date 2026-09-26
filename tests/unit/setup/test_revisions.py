"""Catalog revision fingerprints remain entirely in memory."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, cast

import pytest

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelRoute,
    ModelToolang,
    Provider,
    ProviderToolang,
)
from toolang.setup.revisions import source_content_revision


def test_source_revision_is_deterministic_and_tracks_catalog_route_declarations(
    tmp_path,
):
    def snapshot(api: str) -> ModelCatalogSnapshot:
        model = Model(id="one", name="One", _toolang=ModelToolang(provider="test"))
        provider = Provider(
            id="test",
            name="Test",
            api=api,
            _toolang=ProviderToolang(adapter="responses", env=("TEST_API_KEY",)),
        )
        return ModelCatalogSnapshot(
            providers={"test": provider}, models=(model,), revision="source"
        )

    before = snapshot("https://one.example/v1")
    same = snapshot("https://one.example/v1")
    changed = snapshot("https://two.example/v1")
    assert source_content_revision(before) == source_content_revision(same)
    assert source_content_revision(before) != source_content_revision(changed)
    assert tuple(tmp_path.iterdir()) == ()


def test_catalog_records_detach_readonly_views_from_plugin_owned_data():
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
    cost["input"] = 99
    limits["output"] = 999
    efforts.append("high")
    assert snapshot.models == (model,)
    assert model.cost == {"input": 1}
    assert model.limit == {"output": 100}
    assert model.reasoning_options == ({"values": ("low",)},)


def test_route_detaches_nested_plugin_data():
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
