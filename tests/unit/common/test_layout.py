from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from toolang.common.layout import AgentLayout


def test_resident_layout_derives_agent_paths(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path / "toolang", "alice")

    assert layout.placement == "resident"
    assert layout.home == layout.root / "agents" / "alice"
    assert layout.model_cache == layout.root / ".setup" / "models"
    assert layout.root_state == layout.root / ".state"
    assert layout.home_state == layout.home / ".state"
    assert layout.run_store == layout.home / ".runtime" / "runs.db"
    assert layout.id_state == layout.home / ".runtime" / "ids.json"


def test_visiting_layout_is_stable_and_source_specific() -> None:
    first = AgentLayout.visiting("octo/agents/researcher", "researcher")
    repeated = AgentLayout.visiting("octo/agents/researcher", "researcher")
    other = AgentLayout.visiting("other/agents/researcher", "researcher")

    assert first == repeated
    assert first.placement == "visiting"
    assert first.root.parent == Path("/tmp").resolve()
    assert first.root != other.root


def test_roaming_layout_is_local_to_source(tmp_path: Path) -> None:
    source = tmp_path / "demo.too"
    layout = AgentLayout.roaming(source)

    assert layout.placement == "roaming"
    assert layout.root == tmp_path.resolve() / ".toolang"
    assert layout.name == "demo"
    assert layout.program == layout.root / "agents" / "demo" / "agent.too"


def test_layout_is_immutable(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")

    with pytest.raises(FrozenInstanceError):
        setattr(layout, "name", "bob")


@pytest.mark.parametrize("name", ["", ".", "..", "nested/agent"])
def test_layout_rejects_invalid_agent_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="invalid agent name"):
        AgentLayout.resident(tmp_path, name)
