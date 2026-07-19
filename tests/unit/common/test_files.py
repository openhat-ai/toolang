"""Tests for package-neutral filesystem helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

import toolang.common.files as files
from toolang.common.files import atomic_write_text, file_write_lock


def test_atomic_write_text_replaces_content_and_preserves_mode(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "item.txt"
    path.parent.mkdir()
    path.write_text("before", encoding="utf-8")
    path.chmod(0o640)

    atomic_write_text(path, "after")

    assert path.read_text(encoding="utf-8") == "after"
    assert path.stat().st_mode & 0o777 == 0o640


def test_file_write_lock_is_reentrant(tmp_path: Path) -> None:
    lock_path = tmp_path / "catalog.lock"

    with file_write_lock(lock_path):
        with file_write_lock(lock_path):
            assert lock_path.is_file()


def test_atomic_write_text_cleans_up_after_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "item.txt"

    def fail_replace(_source: str, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(files.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(path, "content")

    assert not path.exists()
    assert tuple(tmp_path.iterdir()) == ()
