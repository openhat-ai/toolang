"""Shared compact duration formatting."""

import pytest

from toolang.common.time import format_duration


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (-1, "0s"),
        (0, "0s"),
        (0.25, "250ms"),
        (0.9996, "1s"),
        (1, "1s"),
        (2.4, "2s"),
        (2.6, "3s"),
        (59, "59s"),
        (59.6, "1m0s"),
        (60, "1m0s"),
        (68, "1m8s"),
        (80, "1m20s"),
        (119.6, "2m0s"),
        (3599.6, "1h0m0s"),
        (3600, "1h0m0s"),
        (3601, "1h0m1s"),
        (3661, "1h1m1s"),
        (90061, "25h1m1s"),
    ],
)
def test_format_duration(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected
