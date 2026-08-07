from decimal import Decimal

import pytest

from toolang.base.types.run import RunLimits
from toolang.cli.common.limits import apply_limit_options


def test_limit_options_overlay_csv_and_repeated_values() -> None:
    limits = apply_limit_options(
        RunLimits(agic_model_calls=100, tokens=1, cost=Decimal("2")),
        ("tokens=500,cost=1.25", "agic_tool_calls=none,time=60"),
    )

    assert limits == RunLimits(
        agic_model_calls=100,
        agic_tool_calls=None,
        tokens=500,
        cost=Decimal("1.25"),
        time=60,
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("unknown=1", "unknown run limit"),
        ("tokens=1,tokens=2", "duplicate run limit"),
        ("tokens=-1", "non-negative integer"),
        ("cost=NaN", "non-negative decimal"),
        ("time", "field=value"),
    ],
)
def test_limit_options_reject_invalid_values(value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        apply_limit_options(RunLimits(), (value,))
