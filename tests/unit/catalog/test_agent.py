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


def test_local_agents_uses_home_directories_as_existence(tmp_path: Path) -> None:
    agents = LocalAgents(tmp_path / "agents")
    empty = agents.path("empty")
    empty.mkdir(parents=True)
    broken = agents.create("broken", content="not valid Toolang source")
    agents.path("file").write_text("not a directory")

    assert agents.list() == ("broken", "empty")
    assert agents.get("empty") == empty
    assert agents.get("broken") == broken
    assert agents.get("file") is None
    with pytest.raises(FileExistsError, match="agent already exists"):
        agents.create("empty", content="agic:\n  Reply directly.\n")
    renamed = agents.rename("empty", "renamed")
    assert renamed.is_dir()
    assert not (renamed / "agent.too").exists()


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
