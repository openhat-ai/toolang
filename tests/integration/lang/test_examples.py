"""Keep the repository examples valid and canonical.

These checks use the language and state APIs directly instead of the CLI, so
they stay offline and deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import PROJECT_ROOT
from toolang.lang import Program, format_source
from toolang.state.state import program_runnable_index


EXAMPLES_ROOT = PROJECT_ROOT / "examples"
# Direct scripts at the top level plus direct flow modules under flows/.
# Both levels are explicit so generated state under .toolang/ stays out.
EXAMPLE_PATHS = tuple(
    sorted(
        example
        for pattern in ("*.too", "flows/*.too")
        for example in EXAMPLES_ROOT.glob(pattern)
    )
)


def _example_id(path: Path) -> str:
    return path.name


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_examples_exist() -> None:
    """Fail loudly when discovery breaks instead of skipping every check."""

    assert EXAMPLE_PATHS


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=_example_id)
def test_example_is_valid_program(path: Path) -> None:
    Program.from_source(_source(path))


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=_example_id)
def test_example_is_canonically_formatted(path: Path) -> None:
    source = _source(path)

    assert format_source(source) == source


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=_example_id)
def test_example_exposes_a_runnable(path: Path) -> None:
    program = Program.from_source(_source(path))

    assert program_runnable_index(program)
