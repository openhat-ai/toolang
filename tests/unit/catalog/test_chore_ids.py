"""Every chore name follows the same agent-bound identity rule."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from toolang.catalog import chore_ids
from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.catalog.chore_ids import chore_id
from toolang.catalog.chores import file_chore
from toolang.catalog.errors import CatalogConflictError
from toolang.common.layout import home_edit_lock_path


@pytest.mark.parametrize(
    ("name", "agent", "expected"),
    [
        ("cleanup", "alice", "m3j36zw8"),
        ("cleanup", "bob", "kyv6myyc"),
        ("Cleanup", "alice", "zfdyegpp"),
        ("é", "alice", "7v69yk7b"),
        ("e\u0301", "alice", "jj96bjnn"),
        ("auto_nqvndr4g", "alice", "nwmwpp7x"),
        ("chore-nqvndr4g", "alice", "vmhghw2s"),
    ],
)
def test_chore_id_vectors(name: str, agent: str, expected: str) -> None:
    assert chore_id(name, agent_name=agent) == expected


@pytest.mark.parametrize(
    "name", ["", " cleanup", "cleanup ", ".", "..", "a/b", "a\\b", "a\0b"]
)
def test_invalid_chore_names_fail(name):
    with pytest.raises(ValueError, match="invalid chore name"):
        chore_id(name, agent_name="alice")


@pytest.mark.parametrize("agent", ["", " alice", "alice ", "Alice", "a/b"])
def test_chore_ids_require_a_canonical_agent(agent):
    with pytest.raises(ValueError):
        chore_id("cleanup", agent_name=agent)


@pytest.mark.parametrize("name", ["cleanup", "auto_nqvndr4g", "chore-nqvndr4g"])
def test_identity_tracks_agent_and_name_but_not_content_or_location(name):
    source = ChoreFile.parse("Clean up.", path=Path("first/chores") / f"{name}.md")
    first = file_chore(source, agent_name="alice")
    moved = file_chore(
        replace(source, path=Path("second/chores") / f"{name}.md"), agent_name="alice"
    )
    edited = replace(first, title="Other", body="Changed.", schedule="FREQ=DAILY")
    assert first.id == moved.id == edited.id == source.id_for("alice")
    assert first.id != replace(first, name="other").id
    assert first.id != file_chore(source, agent_name="bob").id
    assert first.thread_id == f"chore_{first.id}"
    assert len(first.id) == 8


def test_catalog_rejects_collisions_before_writing(tmp_path, monkeypatch):
    catalog = AuthoredChores(
        tmp_path, agent_name="alice", lock_path=home_edit_lock_path(tmp_path)
    )
    first = catalog.create(ChoreFile.parse("First.", name="first"))
    monkeypatch.setattr(chore_ids, "sha256", lambda _: sha256(b"same payload"))
    with pytest.raises(CatalogConflictError, match="chore ID collision"):
        catalog.create(ChoreFile.parse("Second.", name="second"))
    assert not catalog.path("second").exists()
    assert first.path is not None and first.path.read_text() == "First."
