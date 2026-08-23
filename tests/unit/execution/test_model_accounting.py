from __future__ import annotations

from decimal import Decimal

from toolang.base.types.model import Model, ModelCatalogSnapshot, ModelTarget, Provider
from toolang.base.types.run import ModelUsage
from toolang.execution.accounting import (
    build_model_accounting,
    cache_hit_ratio,
    selected_usd_cost,
)
from toolang.execution.records import step_noted_from_data, step_noted_to_data
from toolang.execution.types import ModelStepNoted


def test_accounting_prices_cache_and_reasoning_without_double_counting() -> None:
    accounting = build_model_accounting(
        _target(),
        ModelUsage(
            input_tokens=1000,
            output_tokens=200,
            input_uncached_tokens=350,
            input_cache_read_tokens=650,
            output_visible_tokens=50,
            output_reasoning_tokens=150,
        ),
        _catalog(
            {
                "input": Decimal("2"),
                "output": Decimal("10"),
                "cache_read": Decimal("0.2"),
                "reasoning": Decimal("10"),
            }
        ),
    )

    assert accounting is not None and accounting.estimate is not None
    assert accounting.estimate.amount == "0.00283"
    assert accounting.estimate.complete is True
    assert [line.meter for line in accounting.estimate.lines] == [
        "input.cache_read",
        "input.uncached",
        "output.reasoning",
        "output.visible",
    ]
    assert cache_hit_ratio(accounting) == Decimal("0.65")


def test_accounting_selects_context_tier_and_records_match() -> None:
    accounting = build_model_accounting(
        _target(),
        ModelUsage(input_tokens=300, output_tokens=100),
        _catalog(
            {
                "input": 1,
                "output": 2,
                "tiers": [
                    {
                        "input": 3,
                        "output": 4,
                        "tier": {"type": "context", "size": 200},
                    }
                ],
            }
        ),
    )

    assert accounting is not None and accounting.estimate is not None
    assert accounting.pricing is not None
    assert accounting.pricing.match == {"tier": {"type": "context", "size": 200}}
    assert [line.rate for line in accounting.estimate.lines] == ["3", "4"]


def test_provider_reported_cost_wins_but_estimate_remains_auditable() -> None:
    accounting = build_model_accounting(
        _target(),
        ModelUsage(
            input_tokens=10,
            output_tokens=5,
            reported_cost=Decimal("0.03"),
            reported_currency="USD",
        ),
        _catalog({"input": 1, "output": 2}),
    )

    assert accounting is not None
    assert accounting.selected == "reported"
    assert accounting.reported is not None
    assert accounting.estimate is not None
    assert selected_usd_cost(accounting) == Decimal("0.03")


def test_non_usd_reported_cost_is_not_applied_to_usd_limit() -> None:
    accounting = build_model_accounting(
        _target(),
        ModelUsage(
            input_tokens=10,
            output_tokens=5,
            reported_cost=Decimal("2"),
            reported_currency="EUR",
        ),
        _catalog({"input": 1, "output": 2}),
    )

    assert accounting is not None and accounting.selected == "reported"
    assert selected_usd_cost(accounting) is None


def test_unknown_cache_breakdown_marks_estimate_partial() -> None:
    accounting = build_model_accounting(
        _target(),
        ModelUsage(input_tokens=100, output_tokens=10),
        _catalog({"input": 1, "output": 2, "cache_read": Decimal("0.1")}),
    )

    assert accounting is not None and accounting.estimate is not None
    assert accounting.estimate.complete is False
    assert cache_hit_ratio(accounting) is None


def test_accounting_selects_advertised_mode_price() -> None:
    target = _target()
    target = ModelTarget(
        ref=target.ref,
        provider=target.provider,
        name=target.name,
        model=target.model,
        adapter=target.adapter,
        catalog_revision=target.catalog_revision,
        mode="fast",
    )
    catalog = _catalog({"input": 1, "output": 2})
    model = catalog.models[0]
    mode_model = Model(
        provider_id=model.provider_id,
        id=model.id,
        name=model.name,
        experimental={"modes": {"fast": {"cost": {"input": 3, "output": 4}}}},
        cost=model.cost,
    )
    catalog = ModelCatalogSnapshot(
        providers={
            "test": Provider(
                id="test",
                name="Test",
                env=(),
                npm="@ai-sdk/openai",
                models={"one": mode_model},
            )
        },
        models=(mode_model,),
        revision="sha256:catalog",
    )

    accounting = build_model_accounting(
        target,
        ModelUsage(input_tokens=100, output_tokens=50),
        catalog,
    )

    assert accounting is not None and accounting.pricing is not None
    assert accounting.pricing.plan == "fast"
    assert accounting.pricing.match == {"mode": "fast"}
    assert accounting.estimate is not None
    assert [line.rate for line in accounting.estimate.lines] == ["3", "4"]


def test_audio_rates_replace_overlapping_base_token_rates() -> None:
    accounting = build_model_accounting(
        _target(),
        ModelUsage(
            input_tokens=100,
            output_tokens=50,
            input_audio_tokens=20,
            output_audio_tokens=10,
        ),
        _catalog(
            {
                "input": 1,
                "input_audio": 4,
                "output": 2,
                "output_audio": 8,
            }
        ),
    )

    assert accounting is not None and accounting.estimate is not None
    assert [(line.meter, line.quantity) for line in accounting.estimate.lines] == [
        ("input.audio", "20"),
        ("input", "80"),
        ("output.audio", "10"),
        ("output", "40"),
    ]


def test_durable_model_accounting_round_trips_without_repricing() -> None:
    accounting = build_model_accounting(
        _target(),
        ModelUsage(
            input_tokens=100,
            output_tokens=20,
            input_uncached_tokens=40,
            input_cache_read_tokens=60,
            output_visible_tokens=5,
            output_reasoning_tokens=15,
        ),
        _catalog({"input": 2, "output": 10, "cache_read": 1}),
    )
    noted = ModelStepNoted(accounting=accounting)

    data = step_noted_to_data("model", noted)
    restored = step_noted_from_data("model", data)

    assert restored == noted


def test_legacy_model_noted_data_projects_version_zero_accounting() -> None:
    restored = step_noted_from_data(
        "model",
        {
            "tokens": {"input": 12, "output": 3},
            "price": {"input": "0.000001", "output": "0.000002"},
            "cost": "0.000018",
            "state": None,
        },
    )

    assert isinstance(restored, ModelStepNoted)
    assert restored.accounting is not None
    assert restored.accounting.version == 0
    assert restored.accounting.input_tokens == 12
    assert restored.accounting.estimate is not None
    assert restored.accounting.estimate.complete is False


def _target() -> ModelTarget:
    return ModelTarget(
        ref="test/one",
        provider="test",
        name="One",
        model="one",
        adapter="responses",
        catalog_revision="sha256:catalog",
        reasoning={"effort": "high"},
    )


def _catalog(cost: dict[str, object]) -> ModelCatalogSnapshot:
    model = Model(
        provider_id="test",
        id="one",
        name="One",
        reasoning=True,
        modalities={"input": ("text",), "output": ("text",)},
        limit={"context": 1000},
        cost=cost,
    )
    provider = Provider(
        id="test",
        name="Test",
        env=(),
        npm="@ai-sdk/openai",
        models={"one": model},
    )
    return ModelCatalogSnapshot(
        providers={"test": provider},
        models=(model,),
        revision="sha256:catalog",
    )
