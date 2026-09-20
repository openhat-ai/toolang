"""Finite six-digit money without Decimal arithmetic."""

import pytest

from toolang.base.money import (
    MAX_COST,
    add_cost,
    cost_text,
    cost_units,
    normalize_cost,
)


@pytest.mark.parametrize(
    ("value", "units", "text"),
    [
        (0, 0, "0"),
        (0.0000004, 0, "0"),
        (0.0000005, 1, "0.000001"),
        (1.2345675, 1234568, "1.234568"),
        (999_999_999.999999, 999_999_999_999_999, "999999999.999999"),
    ],
)
def test_cost_settlement(value: float, units: int, text: str) -> None:
    assert cost_units(value) == units
    assert cost_text(value) == text
    assert cost_units(normalize_cost(value)) == units


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), 1_000_000_000])
def test_invalid_cost_is_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        normalize_cost(value)


def test_repeated_costs_and_budget_boundaries_are_exact() -> None:
    total = 0.0
    for _ in range(10000):
        total = add_cost(total, 0.000001)
    assert total == 0.01
    assert cost_units(add_cost(0.1, 0.2)) == cost_units(0.3)
    assert cost_units(add_cost(0.3, 0.000001)) > cost_units(0.3)
    with pytest.raises(ValueError):
        add_cost(MAX_COST, 0.000001)
