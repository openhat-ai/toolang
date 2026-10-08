"""Shared pytest command-line options."""

from __future__ import annotations

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Keep the default suite offline: chat must not publish tmux pane marks."""

    os.environ["TOOLANG_TMUX"] = "0"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register opt-in options shared by the test suite."""

    parser.addoption(
        "--live-valkey",
        action="store_true",
        default=False,
        help="run isolated Redis/Valkey and Text terminal checks",
    )
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

    parser.addoption(
        "--live-compact-model",
        action="store",
        default=None,
        metavar="SELECTOR",
        help="run the cross-model compaction check with this distinct compact model",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip opt-in groups unless their explicit pytest option is present."""

    enabled = {
        "live_valkey": config.getoption("--live-valkey") is True,
        "live_docker": config.getoption("--live-docker") is True,
        "live_provider": bool(config.getoption("--live-model")),
    }
    reasons = {
        "live_valkey": "pass --live-valkey to run isolated Redis/Valkey checks",
        "live_docker": "pass --live-docker to run Docker tests",
        "live_provider": "pass --live-model to run real-provider tests",
    }
    for item in items:
        for marker, selected in enabled.items():
            if not selected and item.get_closest_marker(marker) is not None:
                item.add_marker(pytest.mark.skip(reason=reasons[marker]))


@pytest.fixture(autouse=True)
def offline_token_encoding(request):
    """Keep protocol/execution tests independent of downloaded tokenizer data."""
    if request.node.get_closest_marker("live_provider") is not None:
        yield
        return
    import tiktoken

    class Encoding:
        def encode_ordinary(self, text):
            return range((len(text.encode("utf-8")) + 2) // 3)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tiktoken, "get_encoding", lambda name: Encoding())
        yield
