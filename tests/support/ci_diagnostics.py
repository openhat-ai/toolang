"""Opt-in, flushed per-worker journals for CI tests, including fixture teardown."""

from collections.abc import Generator
from datetime import datetime, timezone
import os
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--ci-diagnostics-dir", type=Path, help="write per-worker test journals here"
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    directory: Path | None = item.config.getoption("ci_diagnostics_dir")
    if directory is None:
        return (yield)
    directory.mkdir(parents=True, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with (directory / f"{worker}.log").open("a", buffering=1) as journal:
        print(
            datetime.now(timezone.utc).isoformat(), "START", item.nodeid, file=journal
        )
        try:
            return (yield)
        finally:
            print(
                datetime.now(timezone.utc).isoformat(),
                "FINISH",
                item.nodeid,
                file=journal,
            )
