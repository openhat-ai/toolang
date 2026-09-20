from __future__ import annotations

from dataclasses import fields

import pytest

from toolang.base.types.model import Model, ModelToolang
from toolang.base.types.run import ModelUsage
from toolang.execution.executor import RunLimits
from toolang.execution.executor.limits import _model_accounting
from toolang.execution.records import run_limits_to_data
from toolang.setup import ModelCollection


def test_run_limits_have_one_compact_stable_shape() -> None:
    assert tuple(field.name for field in fields(RunLimits)) == (
        "agic_model_calls",
        "agic_tool_calls",
        "tokens",
        "cost",
        "time",
    )
    assert RunLimits() == RunLimits(agic_model_calls=200)


def test_run_limits_serialize_six_digit_cost_as_decimal_text() -> None:
    limits = RunLimits(
        agic_model_calls=None,
        agic_tool_calls=12,
        tokens=34_567,
        cost=1.23,
        time=90,
    )

    assert run_limits_to_data(limits) == {
        "agic_model_calls": None,
        "agic_tool_calls": 12,
        "tokens": 34_567,
        "cost": "1.23",
        "time": 90,
    }


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("agic_model_calls", -1, ValueError),
        ("agic_tool_calls", True, TypeError),
        ("tokens", 1.5, TypeError),
        ("time", -1, ValueError),
        ("cost", float("NaN"), ValueError),
        ("cost", True, TypeError),
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
        id="model",
        name="Model",
        _toolang=ModelToolang(provider="test", ready=True),
        cost={"input": 1, "output": 2},
    )
    models = ModelCollection((model,))
    assert models.contains(model.ref)

    accounting = _model_accounting(
        model,
        ModelUsage(
            input_tokens=10,
            output_tokens=5,
            reported_cost=2.0,
            reported_currency="EUR",
        ),
    )

    assert accounting.cost == 2e-05
    assert accounting.accounting is not None
    assert accounting.accounting.selected == "estimated"


def test_cost_budget_uses_settled_units_for_live_and_restored_totals() -> None:
    from toolang.execution.executor.limits import (
        _ModelAccounting,
        _RunLimitExceeded,
        _RunLimitState,
    )

    model = Model("one", "One", ModelToolang(provider="test"))
    live = _RunLimitState(RunLimits(cost=0.3))
    restored = _RunLimitState(RunLimits(cost=0.3))
    for amount in (0.1, 0.2):
        live.record_model(model, _ModelAccounting(usage=None, cost=amount))
        restored.restore(input_tokens=None, output_tokens=None, cost=amount)
    restored.check_restored()
    assert live.cost == restored.cost == 0.3
    with pytest.raises(_RunLimitExceeded):
        live.record_model(model, _ModelAccounting(usage=None, cost=0.000001))
    restored.restore(input_tokens=None, output_tokens=None, cost=0.000001)
    with pytest.raises(_RunLimitExceeded):
        restored.check_restored()


def test_existing_decimal_text_budget_records_remain_readable() -> None:
    from toolang.execution.records import run_limits_from_data

    limits = run_limits_from_data({"cost": "1.2300000000"})
    assert limits.cost == 1.23
    assert run_limits_to_data(limits)["cost"] == "1.23"


@pytest.mark.parametrize("value", [True, False])
def test_budget_records_reject_boolean_costs(value: bool) -> None:
    from pydantic import ValidationError

    from toolang.execution.records import run_limits_from_data

    with pytest.raises(ValidationError):
        run_limits_from_data({"cost": value})


def test_fallback_cost_settles_decimal_token_prices_once() -> None:
    from toolang.execution.executor.limits import _model_cost, _TokenPrice

    assert (
        _model_cost(
            ModelUsage(input_tokens=25, output_tokens=0),
            _TokenPrice(input=0.00000058, output=0),
        )
        == 0.000015
    )
