from __future__ import annotations

from dataclasses import fields
from decimal import Decimal

import pytest

from toolang.base.types.model import Model, ModelCatalogSnapshot, ModelTarget, Provider
from toolang.base.types.run import ModelUsage
from toolang.execution.executor import RunLimits
from toolang.execution.executor.limits import _model_accounting
from toolang.execution.records import run_limits_to_data


def test_run_limits_have_one_compact_stable_shape() -> None:
    assert tuple(field.name for field in fields(RunLimits)) == (
        "agic_model_calls",
        "agic_tool_calls",
        "tokens",
        "cost",
        "time",
    )
    assert RunLimits() == RunLimits(agic_model_calls=200)


def test_run_limits_serialize_decimal_cost_without_precision_loss() -> None:
    limits = RunLimits(
        agic_model_calls=None,
        agic_tool_calls=12,
        tokens=34_567,
        cost=Decimal("1.2300"),
        time=90,
    )

    assert run_limits_to_data(limits) == {
        "agic_model_calls": None,
        "agic_tool_calls": 12,
        "tokens": 34_567,
        "cost": "1.2300",
        "time": 90,
    }


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("agic_model_calls", -1, ValueError),
        ("agic_tool_calls", True, TypeError),
        ("tokens", 1.5, TypeError),
        ("time", -1, ValueError),
        ("cost", Decimal("NaN"), ValueError),
        ("cost", 1, TypeError),
    ],
)
def test_run_limits_reject_invalid_values(
    field: str,
    value: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        RunLimits(**{field: value})  # type: ignore[arg-type]


def test_cost_limit_accounting_uses_estimate_for_non_usd_report() -> None:
    model = Model(
        provider_id="test",
        id="model",
        name="Model",
        cost={"input": 1, "output": 2},
    )
    provider = Provider(
        id="test",
        name="Test",
        env=(),
        npm="@ai-sdk/openai-compatible",
        models={model.id: model},
    )
    catalog = ModelCatalogSnapshot(
        providers={provider.id: provider},
        models=(model,),
        revision="sha256:test",
    )
    target = ModelTarget(
        ref="test/model",
        provider="test",
        name="Model",
        model="model",
        adapter="chat_completions",
    )

    accounting = _model_accounting(
        target,
        (),
        ModelUsage(
            input_tokens=10,
            output_tokens=5,
            reported_cost=Decimal("2"),
            reported_currency="EUR",
        ),
        catalog=catalog,
    )

    assert accounting.cost == Decimal("0.00002")
    assert accounting.accounting is not None
    assert accounting.accounting.selected == "estimated"
