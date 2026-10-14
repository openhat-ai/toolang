"""Pure flat chore definitions, stable identities, and serialized authoring."""

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.catalog.errors import CatalogConflictError, CatalogNotFoundError
from toolang.common.ids import chore_id
from toolang.common.layout import home_edit_lock_path


def test_parsing_preserves_source_and_uses_filename_identity(tmp_path: Path) -> None:
    content = (
        "---\nid: legacy\ntitle: Review\ndue: 2026-07-20\n---\nReview carefully.\n"
    )
    path = tmp_path / "review.md"
    item = ChoreFile.parse(content, path=path)
    assert item.name == "review"
    assert item.id == chore_id("review")
    assert item.ref == "chore/review"
    assert item.content == content
    assert item.meta["due"] == "2026-07-20"
    assert item.schedule is None


def test_definition_edits_never_change_identity() -> None:
    item = ChoreFile.parse("Work.", name="cleanup")
    changed = item.patch(
        {"title": "Display title", "body": "New work.", "schedule": "FREQ=DAILY"}
    )
    assert changed.id == item.id
    assert changed.ref == item.ref
    assert changed.schedule == "FREQ=DAILY"
    assert "id" not in changed.meta
    assert changed.patch({"schedule": None}).schedule is None
    with pytest.raises(ValueError, match="unsupported chore fields"):
        item.patch({"status": "done"})


def test_metadata_normalization() -> None:
    item = ChoreFile.parse("Work.", name="cleanup").with_meta(
        {
            "due": date(2026, 7, 20),
            "labels": ("a", "b"),
            "nested": {1: date(2026, 7, 21)},
        }
    )
    assert item.meta == {
        "due": "2026-07-20",
        "labels": ["a", "b"],
        "nested": {"1": "2026-07-21"},
    }
    assert item.with_body("Updated.").meta == item.meta


@pytest.mark.parametrize(
    "name", ["", " ", " cleanup", "../cleanup", "a/b", "a\\b", ".."]
)
def test_unsafe_or_noncanonical_names_fail(name: str) -> None:
    with pytest.raises(ValueError, match="invalid chore name"):
        ChoreFile.parse("Body.", name=name)


def test_name_must_match_source_filename() -> None:
    with pytest.raises(ValueError, match="name must match"):
        ChoreFile.parse("Body.", name="other", path=Path("cleanup.md"))


@pytest.mark.parametrize(
    "metadata", ["title: ' '", "schedule: ' '", "schedule: not-an-rrule", "schedule: 1"]
)
def test_invalid_metadata_fails(metadata: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        ChoreFile.parse(f"---\n{metadata}\n---\nWork.", name="cleanup")


def test_crud_is_flat_and_addressable_by_name_ref_and_id(tmp_path: Path) -> None:
    catalog = AuthoredChores(tmp_path, lock_path=home_edit_lock_path(tmp_path))
    assert catalog.list() == ()
    assert not catalog.lock_path.exists()
    item = catalog.create(ChoreFile.parse("Work.\n", name="cleanup"))
    assert item.path == tmp_path / "chores" / "cleanup.md"
    for reference in (item.name, item.ref, item.id):
        assert catalog.get(reference) == item
    changed = catalog.update(item.patch({"body": "Updated."}))
    assert changed.id == item.id
    assert catalog.remove(item.ref) == changed
    assert catalog.list() == ()
    assert item.path is not None and not item.path.exists()
    assert catalog.create(item).content == "Work.\n"


def test_authoring_rejects_conflicts_and_missing_definitions(tmp_path: Path) -> None:
    catalog = AuthoredChores(tmp_path, lock_path=home_edit_lock_path(tmp_path))
    item = catalog.create(ChoreFile.parse("Work.", name="cleanup"))
    with pytest.raises(CatalogConflictError, match="already exists"):
        catalog.create(item)
    with pytest.raises(CatalogNotFoundError):
        catalog.update(replace(item, name="missing"))
    with pytest.raises(CatalogNotFoundError):
        catalog.remove("missing")


def test_only_immediate_files_are_effective_and_reads_do_not_write(
    tmp_path: Path,
) -> None:
    paths = [
        tmp_path / relative
        for relative in (
            "chores/current.md",
            "chores/nested/hidden.md",
            "drafts/chores/old.md",
            "archive/chores/old.md",
        )
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Work.\n")
    before = {path: path.read_bytes() for path in paths}
    catalog = AuthoredChores(tmp_path, lock_path=home_edit_lock_path(tmp_path))
    assert [item.name for item in catalog.list()] == ["current"]
    assert catalog.get("current") is not None
    assert not catalog.lock_path.exists()
    assert before == {path: path.read_bytes() for path in paths}


def test_authoring_lock_is_reentrant(tmp_path: Path) -> None:
    catalog = AuthoredChores(tmp_path, lock_path=home_edit_lock_path(tmp_path))
    with catalog.write_lock():
        item = catalog.create(ChoreFile.parse("Work.", name="cleanup"))
        catalog.update(item.with_body("Updated."))
    assert catalog.lock_path == home_edit_lock_path(tmp_path)
