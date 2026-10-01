"""Inspection query views are projected directly from in-memory catalog data."""

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelRoute,
    ModelToolang,
    Provider,
)
from toolang.plugin.models.query import filter_models
from toolang.plugin.models.records import model_record


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
    assert tuple(model_record(model)["ref"] for model in snapshot.models) == (
        "openai/ready",
        "openai/unready",
        "other/ready",
    )
    assert tuple(
        model.ref for model in filter_models(snapshot.models, ("*[tags has ready]",))
    ) == (
        "openai/ready",
        "other/ready",
    )
    assert model_record(snapshot.models[1])["tags"] == [
        "no_env",
        "no_api",
        "no_adapter",
        "remote",
    ]
