from __future__ import annotations

from dataclasses import fields
from decimal import Decimal

import pytest

from toolang.execution.executor import RunLimits
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
