"""Keep the repository examples valid and canonical.

These checks use the language and state APIs directly instead of the CLI, so
they stay offline and deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import PROJECT_ROOT
from tests.support.examples import example_sources
from toolang.lang import Program, format_source
from toolang.execution.runnables import resolve_runnable_reference
from toolang.state.state import flow_export, program_runnable_index


EXAMPLES_ROOT = PROJECT_ROOT / "examples"
EXAMPLE_PATHS = example_sources(EXAMPLES_ROOT)
FLOW_MODULE_PATHS = tuple(
    path for path in EXAMPLE_PATHS if path.parent == EXAMPLES_ROOT / "flows"
)


def _example_id(path: Path) -> str:
    return path.name


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_examples_exist() -> None:
    """Fail loudly when discovery breaks instead of skipping every check."""

    assert EXAMPLE_PATHS


def test_example_discovery_excludes_generated_state(tmp_path: Path) -> None:
    authored = (tmp_path / "hello.too", tmp_path / "flows" / "worker.too")
    for path in authored:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("flow:\n  run: Hello.\n", encoding="utf-8")
    for root in (tmp_path, tmp_path / "flows"):
        cache = root / ".toolang" / "agents" / "worker"
        snapshot = cache / ".state" / "home" / "revs" / "old" / "files"
        snapshot.mkdir(parents=True)
        (snapshot / "agent.too").write_text(
            "flow:\n  scatter using worker\n", encoding="utf-8"
        )
        (cache / "agent.too").symlink_to(cache / "missing.too")

    assert example_sources(tmp_path) == tuple(sorted(authored))


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=_example_id)
def test_example_is_canonically_formatted(path: Path) -> None:
    source = _source(path)

    assert format_source(source) == source


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=_example_id)
def test_example_exposes_a_runnable(path: Path) -> None:
    program = Program.from_source(_source(path))

    assert program_runnable_index(program)


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=_example_id)
def test_example_route_directives_resolve(path: Path) -> None:
    """Catch route typos, which stay valid syntax as unresolved authored refs."""

    program = Program.from_source(_source(path))

    for agic in program.agics:
        for directive in agic.directives:
            if directive.name in {"hands", "handoffs"}:
                for reference in directive.values:
                    if reference not in {"*", "none"}:
                        assert resolve_runnable_reference(program, reference)


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=_example_id)
def test_example_resolves_a_default_entry(path: Path) -> None:
    """Every example must run both as a script and as an agent surface."""

    program = Program.from_source(_source(path))
    names = program_runnable_index(program)

    assert any(name.startswith("<entry:") or name == "chat" for name in names)


@pytest.mark.parametrize("path", FLOW_MODULE_PATHS, ids=_example_id)
def test_flow_module_exports_exactly_one_flow(path: Path) -> None:
    """A flow module must export one flow, named by its filename stem."""

    program = Program.from_source(_source(path))

    # Raises when the module does not export exactly one qualifying flow.
    flow_export(path.name, program)
