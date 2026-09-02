from __future__ import annotations

from decimal import Decimal

from toolang.base.types.model import Model, ModelCatalogSnapshot, ModelTarget, Provider
from toolang.base.types.run import ModelUsage
from toolang.execution.accounting import (
    build_model_accounting,
    cache_hit_ratio,
    selected_cost_is_approximate,
    selected_usd_cost,
    token_meter_quantity,
)
from toolang.execution.records import step_noted_from_data, step_noted_to_data
from toolang.execution.types import ModelAccounting, ModelStepNoted, ModelUsageMeter


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
    assert token_meter_quantity(accounting, "output.reasoning") == 150


def test_token_meter_quantity_treats_ambiguous_or_invalid_values_as_unknown() -> None:
    duplicate = ModelAccounting(
        input_tokens=0,
        output_tokens=2,
        meters=(
            ModelUsageMeter("output.reasoning", "1", "token"),
            ModelUsageMeter("output.reasoning", "1", "token"),
        ),
    )
    fractional = ModelAccounting(
        input_tokens=0,
        output_tokens=1,
        meters=(ModelUsageMeter("output.reasoning", "0.5", "token"),),
    )
    other_unit = ModelAccounting(
        input_tokens=0,
        output_tokens=1,
        meters=(ModelUsageMeter("output.reasoning", "1", "request"),),
    )

    assert token_meter_quantity(duplicate, "output.reasoning") is None
    assert token_meter_quantity(fractional, "output.reasoning") is None
    assert token_meter_quantity(other_unit, "output.reasoning") is None


def test_accounting_accepts_fractional_float_rates_from_effective_resources() -> None:
    accounting = build_model_accounting(
        _target(),
        ModelUsage(input_tokens=1000, output_tokens=100),
        _catalog({"input": 0.14, "output": 0.28}),
    )

    assert accounting is not None and accounting.estimate is not None
    assert accounting.estimate.amount == "0.000168"
    assert accounting.estimate.complete is True
    assert [line.rate for line in accounting.estimate.lines] == ["0.14", "0.28"]


def test_accounting_prices_cache_writes_as_a_distinct_input_meter() -> None:
    accounting = build_model_accounting(
        _target(),
        ModelUsage(
            input_tokens=100,
            output_tokens=20,
            input_uncached_tokens=30,
            input_cache_read_tokens=60,
            input_cache_write_tokens=10,
        ),
        _catalog(
            {
                "input": Decimal("2"),
                "output": Decimal("10"),
                "cache_read": Decimal("0.2"),
                "cache_write": Decimal("2.5"),
            }
        ),
    )

    assert accounting is not None and accounting.estimate is not None
    assert accounting.estimate.complete is True
    assert [
        (line.meter, line.quantity, line.rate) for line in accounting.estimate.lines
    ] == [
        ("input.cache_read", "60", "0.2"),
        ("input.cache_write", "10", "2.5"),
        ("input.uncached", "30", "2"),
        ("output", "20", "10"),
    ]


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


def test_zero_prices_produce_a_complete_zero_cost() -> None:
    accounting = build_model_accounting(
        _target(),
        ModelUsage(input_tokens=100, output_tokens=50),
        _catalog({"input": 0, "output": 0}),
    )

    assert accounting is not None and accounting.estimate is not None
    assert accounting.estimate.amount == "0"
    assert accounting.estimate.complete is True
    assert [line.rate for line in accounting.estimate.lines] == ["0", "0"]
    assert selected_usd_cost(accounting) == Decimal("0")


def test_local_zero_price_remains_exact_when_cache_usage_is_reported() -> None:
    model = Model(
        provider_id="llama_cpp",
        id="local",
        name="Local",
        cost={"input": 0, "output": 0},
        local=True,
    )
    catalog = ModelCatalogSnapshot(
        providers={
            "llama_cpp": Provider(
                id="llama_cpp",
                name="llama.cpp",
                env=(),
                npm="@ai-sdk/openai-compatible",
                models={model.id: model},
                local=True,
            )
        },
        models=(model,),
        revision="runtime:local",
    )
    accounting = build_model_accounting(
        ModelTarget(
            ref="llama_cpp/local",
            provider="llama_cpp",
            name="Local",
            model="local",
            adapter="chat_completions",
            catalog="llama_cpp",
            catalog_revision="runtime:local",
        ),
        ModelUsage(
            input_tokens=4100,
            output_tokens=15,
            input_uncached_tokens=123,
            input_cache_read_tokens=3977,
        ),
        catalog,
    )

    assert accounting is not None and accounting.estimate is not None
    assert accounting.estimate.amount == "0"
    assert accounting.estimate.complete is True
    assert selected_cost_is_approximate(accounting) is False


def test_non_usd_reported_cost_falls_back_to_catalog_usd_estimate() -> None:
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

    assert accounting is not None and accounting.selected == "estimated"
    assert accounting.reported is not None
    assert accounting.reported.amount == "2"
    assert accounting.reported.currency == "EUR"
    assert accounting.estimate is not None
    assert selected_usd_cost(accounting) == Decimal("0.00002")
    assert selected_cost_is_approximate(accounting) is True


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


def test_accounting_records_unsupported_billing_context_as_partial() -> None:
    accounting = build_model_accounting(
        _target(),
        ModelUsage(
            input_tokens=100,
            output_tokens=20,
            billing={"service_tier": "priority", "inference_geo": "us"},
        ),
        _catalog({"input": 1, "output": 2}),
    )

    assert accounting is not None and accounting.estimate is not None
    assert accounting.estimate.complete is False
    assert accounting.pricing is not None
    assert accounting.pricing.match == {
        "billing": {"inference_geo": "us", "service_tier": "priority"}
    }
    assert accounting.reasoning.requested == {"effort": "high"}
    assert accounting.reasoning.selected is None


def test_accounting_accepts_standard_billing_context() -> None:
    accounting = build_model_accounting(
        _target(),
        ModelUsage(
            input_tokens=100,
            output_tokens=20,
            billing={"service_tier": "standard", "traffic_type": "on_demand"},
        ),
        _catalog({"input": 1, "output": 2}),
    )

    assert accounting is not None and accounting.estimate is not None
    assert accounting.estimate.complete is True


def test_accounting_preserves_billing_context_without_catalog_price() -> None:
    accounting = build_model_accounting(
        _target(),
        ModelUsage(
            input_tokens=100,
            output_tokens=20,
            reported_cost=Decimal("0.03"),
            reported_currency="USD",
            billing={"service_tier": "priority"},
        ),
        None,
    )

    assert accounting is not None and accounting.pricing is not None
    assert accounting.pricing.source == "unknown"
    assert accounting.pricing.revision == "sha256:catalog"
    assert accounting.pricing.match == {"billing": {"service_tier": "priority"}}
    assert accounting.reported is not None
    assert accounting.estimate is None


def test_accounting_uses_model_catalog_provenance_without_inventing_reasoning() -> None:
    model = Model(
        provider_id="ollama",
        id="local",
        name="Local",
        reasoning=True,
        cost={"input": 0, "output": 0},
        catalog="ollama",
        catalog_revision="runtime:local",
        local=True,
    )
    catalog = ModelCatalogSnapshot(
        providers={
            "ollama": Provider(
                id="ollama",
                name="Ollama",
                env=(),
                npm="@ai-sdk/openai-compatible",
                models={model.id: model},
                catalog="ollama",
                catalog_revision="runtime:local",
                local=True,
            )
        },
        models=(model,),
        revision="sha256:merged",
    )
    target = ModelTarget(
        ref="ollama/local",
        provider="ollama",
        name="Local",
        model="local",
        adapter="chat_completions",
        catalog="ollama",
        catalog_revision="runtime:local",
        reasoning={"effort": "high"},
    )

    accounting = build_model_accounting(
        target,
        ModelUsage(input_tokens=10, output_tokens=5),
        catalog,
    )

    assert accounting is not None and accounting.pricing is not None
    assert accounting.pricing.source == "ollama"
    assert accounting.pricing.revision == "runtime:local"
    assert accounting.reasoning.requested == {"effort": "high"}
    assert accounting.reasoning.selected is None


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
            "cont": None,
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
