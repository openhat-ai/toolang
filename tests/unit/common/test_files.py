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


def test_file_write_lock_notifies_only_while_waiting_for_another_thread(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    path = tmp_path / "catalog.lock"
    waiting = Event()
    calls = []

    def on_wait():
        calls.append("waiting")
        waiting.set()

    def acquire():
        with file_write_lock(path, on_wait=on_wait):
            calls.append("acquired")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_write_lock(path, on_wait=on_wait):
            with file_write_lock(path, on_wait=on_wait):
                assert calls == []
            future = pool.submit(acquire)
            assert waiting.wait(timeout=2)
            assert calls == ["waiting"]
        future.result(timeout=2)
    assert calls == ["waiting", "acquired"]
    with file_write_lock(path, on_wait=on_wait):
        assert calls == ["waiting", "acquired"]


def test_file_write_lock_reports_process_contention_and_ignores_sink_errors(
    tmp_path, monkeypatch
):
    calls = []

    def flock(_fd, flags):
        calls.append(flags)
        if flags & files.fcntl.LOCK_NB:
            raise BlockingIOError()

    def on_wait():
        calls.append("waiting")
        raise RuntimeError("presentation failed")

    monkeypatch.setattr(files.fcntl, "flock", flock)
    with file_write_lock(tmp_path / "catalog.lock", on_wait=on_wait):
        assert calls == [
            files.fcntl.LOCK_EX | files.fcntl.LOCK_NB,
            "waiting",
            files.fcntl.LOCK_EX,
        ]
    assert calls[-1] == files.fcntl.LOCK_UN
