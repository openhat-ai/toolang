from __future__ import annotations

import pytest

from toolang.base.types.run import ModelUsage


def test_known_input_components_cannot_exceed_inclusive_total() -> None:
    with pytest.raises(ValueError, match="components exceed total input"):
        ModelUsage(
            input_tokens=100,
            output_tokens=10,
            input_uncached_tokens=80,
            input_cache_read_tokens=60,
        )


def test_partial_input_components_can_leave_an_unknown_remainder() -> None:
    usage = ModelUsage(
        input_tokens=100,
        output_tokens=10,
        input_uncached_tokens=30,
        input_cache_write_tokens=10,
    )

    assert usage.input_cache_read_tokens is None
