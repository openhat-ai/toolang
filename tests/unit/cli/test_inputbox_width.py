"""Configuration for the shared Chat/Talk input area."""

import pytest

from toolang.cli.common.input import resolve_inputbox_max_width


@pytest.mark.parametrize("fallback", [72, 120])
def test_unset_inputbox_width_uses_the_resolved_progress_limit(fallback):
    assert resolve_inputbox_max_width({}, fallback=fallback) == fallback


@pytest.mark.parametrize("value", [" 40 ", "160"])
def test_inputbox_width_can_be_smaller_or_larger_than_output(value):
    assert resolve_inputbox_max_width(
        {"TOOLANG_INPUTBOX_MAX_WIDTH": value}, fallback=120
    ) == int(value)


@pytest.mark.parametrize("value", ["", "wide", "0", "-1", "1.5"])
def test_invalid_inputbox_width_names_the_setting(value):
    with pytest.raises(
        ValueError, match="TOOLANG_INPUTBOX_MAX_WIDTH must be a positive integer"
    ):
        resolve_inputbox_max_width({"TOOLANG_INPUTBOX_MAX_WIDTH": value}, fallback=120)
