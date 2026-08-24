"""Unit tests for authored resident-agent homes."""

from __future__ import annotations

from pathlib import Path

import pytest

from toolang.catalog.agent import LocalAgents


def test_local_agents_crud_returns_home_paths(tmp_path: Path) -> None:
    directory = tmp_path / "agents"
    agents = LocalAgents(directory)

    alice = agents.create("alice", content="agent source\n")
    bob = agents.create("bob", content="agent bob\n")

    assert alice == directory / "alice"
    assert (alice / "agent.too").read_text(encoding="utf-8") == "agent source\n"
    assert (alice / "collab" / "MEMO.md").read_text(encoding="utf-8") == "\n"
    assert (alice / "lab" / "MEMO.md").read_text(encoding="utf-8") == "\n"
    assert agents.list() == ("alice", "bob")
    assert agents.get("bob") == bob

    renamed = agents.rename("bob", "robert")
    assert renamed == directory / "robert"
    assert (renamed / "agent.too").read_text(encoding="utf-8") == "agent bob\n"

    removed = agents.remove("robert")
    assert removed == renamed
    assert not removed.exists()
    assert agents.get("robert") is None


def test_local_agents_rejects_nested_names(tmp_path: Path) -> None:
    agents = LocalAgents(tmp_path / "agents")

    with pytest.raises(ValueError, match="invalid agent name"):
        agents.create("team/alice", content="agent alice\n")


def test_local_agents_reports_missing_and_conflicting_mutations(tmp_path: Path) -> None:
    agents = LocalAgents(tmp_path / "agents")

    assert agents.list() == ()
    with pytest.raises(FileNotFoundError, match="agent not found"):
        agents.rename("missing", "other")
    with pytest.raises(FileNotFoundError, match="agent not found"):
        agents.remove("missing")

    agents.create("alice", content="agent source\n")
    agents.create("bob", content="agent other\n")
    with pytest.raises(FileExistsError, match="agent already exists"):
        agents.create("alice", content="duplicate\n")
    with pytest.raises(FileExistsError, match="agent already exists"):
        agents.rename("alice", "bob")


@pytest.mark.parametrize("name", ["", " ", ".", "..", "team/alice", "team\\alice"])
def test_local_agents_rejects_invalid_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="invalid agent name"):
        LocalAgents(tmp_path / "agents").path(name)


def test_materialize_runspaces_preserves_notes_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    from toolang.catalog.agent import materialize_agent_runspaces
    from toolang.common.layout import AgentLayout

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.collab.mkdir()
    layout.collab_memo.write_text("remember\n", encoding="utf-8")

    materialize_agent_runspaces(layout)
    materialize_agent_runspaces(layout)

    assert layout.collab_memo.read_text(encoding="utf-8") == "remember\n"
    assert layout.lab_memo.read_text(encoding="utf-8") == "\n"

    layout.lab_memo.unlink()
    layout.lab_memo.mkdir()
    with pytest.raises(FileExistsError, match="memo path is not a file"):
        materialize_agent_runspaces(layout)
