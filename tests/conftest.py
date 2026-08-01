"""Shared pytest command-line options."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register opt-in options shared by the test suite."""

    parser.addoption(
        "--live-model",
        action="store",
        default=None,
        metavar="SELECTOR",
        help=(
            "run live model-provider smoke tests with this selector, for example "
            "'deepseek/deepseek-chat[deepseek]'"
        ),
    )
