from __future__ import annotations

import pytest

from toolang.cli.common.execution_progress.config import (
    DEFAULT_MAX_PROGRESS_WIDTH,
    resolve_progress_max_width,
)


def test_progress_max_width_defaults_to_120() -> None:
    assert resolve_progress_max_width({}) == DEFAULT_MAX_PROGRESS_WIDTH == 120


def test_progress_max_width_accepts_a_positive_environment_override() -> None:
    assert resolve_progress_max_width({"TOOLANG_PROGRESS_MAX_WIDTH": " 88 "}) == 88


@pytest.mark.parametrize("value", ["", "wide", "0", "-1"])
def test_progress_max_width_rejects_invalid_environment_values(value: str) -> None:
    with pytest.raises(
        ValueError,
        match="TOOLANG_PROGRESS_MAX_WIDTH must be a positive integer",
    ):
        resolve_progress_max_width({"TOOLANG_PROGRESS_MAX_WIDTH": value})
