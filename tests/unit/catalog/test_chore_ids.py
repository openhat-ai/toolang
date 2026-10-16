"""Agent-bound chore identity and manual/automatic namespace contracts."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from toolang.catalog import chore_ids
from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.catalog.chore_ids import automatic_chore_name, chore_id
from toolang.catalog.chores import file_chore
from toolang.catalog.errors import CatalogConflictError
from toolang.common.id_codec import decode_short_id, derive_id_key, scramble_id
from toolang.common.layout import home_edit_lock_path


@pytest.mark.parametrize(
    ("name", "agent", "expected"),
    [
        ("cleanup", "alice", "m3j36zw8"),
        ("cleanup", "bob", "kyv6myyc"),
        ("Cleanup", "alice", "5m2p4n9k"),
        ("é", "alice", "7v69yk7b"),
        ("e\u0301", "alice", "nz1gdzak"),
        ("auto_01234567", "alice", "2yfv9tq5"),
        ("auto_01234567", "bob", "5eqqj28b"),
    ],
)
def test_chore_id_vectors(name: str, agent: str, expected: str) -> None:
    assert chore_id(name, agent_name=agent) == expected


def test_manual_and_automatic_names_remain_disjoint_on_equal_hashes(monkeypatch):
    monkeypatch.setattr(chore_ids, "sha256", lambda _: sha256(b"same payload"))
    names = ("cleanup", automatic_chore_name("01234567"))
    ids = [chore_id(name, agent_name="alice") for name in names]
    assert ids[0] != ids[1]
    raw = [
        scramble_id(
            decode_short_id(identifier),
            width=8,
            key=derive_id_key("toolang:chore:v1", "alice"),
            reverse=True,
        )
        for identifier in ids
    ]
    assert raw[0] >> 39 == 0
    assert raw[1] >> 39 == 1
    assert raw[0] == raw[1] & ((1 << 39) - 1)


@pytest.mark.parametrize("suffix", ["", "0123456", "012345678", "ABCDEFGH", "iiiiiiii"])
def test_automatic_names_require_canonical_allocated_suffixes(suffix):
    with pytest.raises(ValueError, match="invalid automatic chore suffix"):
        automatic_chore_name(suffix)


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


def test_identity_tracks_agent_and_name_but_not_content_or_location():
    source = ChoreFile.parse("Clean up.", path=Path("first/chores/cleanup.md"))
    first = file_chore(source, agent_name="alice")
    moved = file_chore(
        replace(source, path=Path("second/chores/cleanup.md")), agent_name="alice"
    )
    edited = replace(first, title="Other", body="Changed.", schedule="FREQ=DAILY")
    assert first.id == moved.id == edited.id == source.id_for("alice")
    assert first.id != replace(first, name="other").id
    assert first.id != file_chore(source, agent_name="bob").id
    assert first.thread_id == f"chore_{first.id}"
    assert len(first.thread_id.removeprefix("chore_")) == 8


def test_catalog_rejects_same_domain_collisions_before_writing(tmp_path, monkeypatch):
    catalog = AuthoredChores(
        tmp_path, agent_name="alice", lock_path=home_edit_lock_path(tmp_path)
    )
    first = catalog.create(ChoreFile.parse("First.", name="first"))
    monkeypatch.setattr(chore_ids, "sha256", lambda _: sha256(b"same payload"))
    with pytest.raises(CatalogConflictError, match="chore ID collision"):
        catalog.create(ChoreFile.parse("Second.", name="second"))
    assert not catalog.path("second").exists()
    assert first.path is not None and first.path.read_text() == "First."
