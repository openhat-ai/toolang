"""Shared pytest command-line options."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register opt-in options shared by the test suite."""

    parser.addoption(
        "--live-docker",
        action="store_true",
        default=False,
        help="run tests that use the local Docker engine and network",
    )
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


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip opt-in groups unless their explicit pytest option is present."""

    enabled = {
        "live_docker": config.getoption("--live-docker") is True,
        "live_provider": bool(config.getoption("--live-model")),
    }
    reasons = {
        "live_docker": "pass --live-docker to run Docker tests",
        "live_provider": "pass --live-model to run real-provider tests",
    }
    for item in items:
        for marker, selected in enabled.items():
            if not selected and item.get_closest_marker(marker) is not None:
                item.add_marker(pytest.mark.skip(reason=reasons[marker]))
