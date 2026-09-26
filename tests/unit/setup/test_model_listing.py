"""Inspection query views are projected directly from in-memory catalog data."""

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelRoute,
    ModelToolang,
    Provider,
)
from toolang.plugin.models.collections import catalog_model_dataset


def _model(ref: str, *, ready: bool) -> Model:
    provider, _, model_id = ref.partition("/")
    return Model(
        id=model_id,
        name=model_id,
        tool_call=True,
        _toolang=ModelToolang(
            provider=provider,
            ready=ready,
            route=ModelRoute(
                adapter="responses" if ready else None,
                api="https://example.test/v1" if ready else None,
                env=() if ready else None,
            ),
        ),
    )


def test_model_query_dataset_projects_full_catalog_records():
    snapshot = ModelCatalogSnapshot(
        providers={
            "openai": Provider(id="openai", name="OpenAI"),
            "other": Provider(id="other", name="Other"),
        },
        models=(
            _model("openai/ready", ready=True),
            _model("openai/unready", ready=False),
            _model("other/ready", ready=True),
        ),
        revision="revision",
    )
    dataset = catalog_model_dataset(snapshot)
    assert tuple(item.key for item in dataset.query(None)) == (
        "openai/ready",
        "openai/unready",
        "other/ready",
    )
    assert tuple(item.key for item in dataset.query("*[available]")) == (
        "openai/ready",
        "other/ready",
    )
    assert dataset.table(dataset.query("openai/unready"))[1][0][0] == "openai/unready"
