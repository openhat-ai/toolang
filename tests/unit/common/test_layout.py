from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from toolang.common.layout import (
    AgentLayout,
    derive_agent_name,
    ensure_scratch_workspace,
    validate_agent_name,
)


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
    assert layout.root == layout.place == (tmp_path / "toolang").resolve()
    assert layout.home == layout.place / "agents" / "alice"
    assert layout.place_state == layout.place / ".state" / "root"
    assert layout.home_state == layout.home / ".state" / "home"
    assert layout.agent_state == layout.home / ".state" / "agent"
    assert layout.sandbox_home == layout.place / ".sandbox" / "alice"
    assert layout.runtime_status == layout.home / ".runtime" / "status.json"
    assert layout.previous_sandbox_state == layout.sandbox_home / "state.json"
    assert layout.legacy_sandbox_state == layout.runtime / "sandbox.json"
    assert layout.sandbox_stage == layout.sandbox_home / "launches"
    assert layout.runtime_status.is_relative_to(layout.home)
    assert layout.run_store == layout.home / ".runtime" / "runs.db"
    assert layout.stats_store == layout.home / ".runtime" / "stats.db"


def test_visiting_layout_is_stable_and_root_and_source_specific(
    tmp_path: Path, isolated_visiting_base: Path
) -> None:
    root = tmp_path / "toolang"
    first = AgentLayout.visiting("gh:octo/agents/researcher", "researcher", root=root)
    repeated = AgentLayout.visiting(
        "gh:octo/agents/researcher", "researcher", root=root
    )
    other = AgentLayout.visiting("gh:other/agents/researcher", "researcher", root=root)
    other_root = AgentLayout.visiting(
        "gh:octo/agents/researcher", "researcher", root=tmp_path / "other"
    )

    assert first == repeated
    assert first.placement == "visiting"
    assert first.root == root.resolve()
    assert first.home == first.place / "agents" / first.name
    assert first.place.parent.parent == isolated_visiting_base
    assert first.source_id is not None and len(first.source_id) == 10
    assert first.place.name == f"t{first.source_id}"
    assert first.place != other.place
    assert first.place != other_root.place
    assert first.root != other_root.root
    assert first.place_code == other_root.place_code


@pytest.mark.parametrize("name_length", [1, 64])
def test_visiting_root_preserves_bounded_agent_names(
    tmp_path: Path, name_length: int
) -> None:
    name = "a" * name_length
    layout = AgentLayout.visiting(
        f"https://example.com/{name}.too", name, root=tmp_path
    )

    (tmp_path / layout.place.name).mkdir()

    assert layout.name == name
    assert layout.place.name == f"t{layout.source_id}"
    assert layout.home.name == name
    assert layout.source_id is not None and len(layout.source_id) == 10


def test_roaming_layout_is_local_to_source(tmp_path: Path) -> None:
    source = tmp_path / "demo.too"
    layout = AgentLayout.roaming(source)

    assert layout.placement == "roaming"
    assert layout.place == tmp_path.resolve() / ".toolang"
    assert layout.name == "demo"
    assert layout.program == layout.place / "agents" / "demo" / "agent.too"


def test_layout_is_immutable(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")

    with pytest.raises(FrozenInstanceError):
        setattr(layout, "name", "bob")


@pytest.mark.parametrize("name", ["", ".", "..", "nested/agent"])
def test_layout_rejects_invalid_agent_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="invalid agent name"):
        AgentLayout.resident(tmp_path, name)


@pytest.mark.parametrize("name", ["a", "a" * 64, "daily_report", "a0", "a__"])
def test_explicit_agent_names_are_preserved(name):
    assert validate_agent_name(name) == name
    assert derive_agent_name(name) == name


@pytest.mark.parametrize(
    "name", ["a" * 65, "Gus", "gus-by3", "a.b", "_a", "1a", "a ", "中文", ""]
)
def test_explicit_agent_names_are_not_normalized(name):
    with pytest.raises(ValueError, match="invalid agent name"):
        validate_agent_name(name)


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("Daily-Report.v2", "daily_report_v2"),
        ("2026 report", "script_2026_report"),
        ("Some 中文 Name", "some_name"),
        ("__Gus__", "gus"),
    ],
)
def test_derived_names_normalize_characters(stem, expected):
    assert derive_agent_name(stem) == expected


@pytest.mark.parametrize("stem", ["中文", "---", "A" * 65, "1" * 58])
def test_derived_names_reject_empty_or_overlong_results(stem):
    with pytest.raises(ValueError, match="invalid agent name"):
        derive_agent_name(stem)
