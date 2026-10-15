from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from toolang.common.layout import AgentLayout, ensure_scratch_workspace


@pytest.mark.parametrize("existing", [False, True])
def test_scratch_workspace_uses_hidden_directory(
    tmp_path: Path, existing: bool
) -> None:
    home = tmp_path / "agents" / "alice"
    home.mkdir(parents=True)
    scratch = home / ".lab"
    if existing:
        scratch.mkdir()
        (scratch / "marker").write_text("keep")

    assert ensure_scratch_workspace(home) == scratch
    assert scratch.is_dir()
    assert not (home / "lab").exists()
    if existing:
        assert (scratch / "marker").read_text() == "keep"


def test_resident_layout_derives_agent_paths(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path / "toolang", "alice")

    assert layout.placement == "resident"
    assert layout.home == layout.root / "agents" / "alice"
    assert layout.root_state == layout.root / ".state" / "root"
    assert layout.home_state == layout.home / ".state" / "home"
    assert layout.agent_state == layout.home / ".state" / "agent"
    assert layout.sandbox_home == layout.root / ".sandbox" / "alice"
    assert layout.runtime_status == layout.home / ".runtime" / "status.json"
    assert layout.previous_sandbox_state == layout.sandbox_home / "state.json"
    assert layout.legacy_sandbox_state == layout.runtime / "sandbox.json"
    assert layout.sandbox_stage == layout.sandbox_home / "launches"
    assert layout.runtime_status.is_relative_to(layout.home)
    assert layout.run_store == layout.home / ".runtime" / "runs.db"


def test_visiting_layout_is_stable_and_root_and_source_specific(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    first = AgentLayout.visiting(
        "octo/agents/researcher", "researcher", toolang_root=root
    )
    repeated = AgentLayout.visiting(
        "octo/agents/researcher", "researcher", toolang_root=root
    )
    other = AgentLayout.visiting(
        "other/agents/researcher", "researcher", toolang_root=root
    )
    other_root = AgentLayout.visiting(
        "octo/agents/researcher", "researcher", toolang_root=tmp_path / "other"
    )

    assert first == repeated
    assert first.placement == "visiting"
    assert first.root.parent == Path("/tmp").resolve()
    assert first.source_id is not None and len(first.source_id) == 16
    assert first.root.name == f"toolang-researcher-{first.source_id}"
    assert first.root != other.root
    assert first.root != other_root.root


@pytest.mark.parametrize("name_length", [1, 64])
def test_visiting_root_preserves_bounded_agent_names(
    tmp_path: Path, name_length: int
) -> None:
    name = "a" * name_length
    layout = AgentLayout.visiting(
        f"https://example.com/{name}.too", name, toolang_root=tmp_path
    )

    (tmp_path / layout.root.name).mkdir()

    assert layout.name == name
    assert layout.root.name.endswith(f"-{layout.source_id}")
    assert layout.source_id is not None and len(layout.source_id) == 16


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
