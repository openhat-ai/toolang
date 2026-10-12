"""Legacy CLI persistence is imported completely without replacing a winner."""

from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier

import pytest

from toolang.cli.common.input_history import import_legacy_file


@pytest.mark.parametrize("existing", [b"", b"current"])
def test_legacy_import_keeps_existing_destination(tmp_path, existing):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.write_bytes(b"legacy")
    destination.write_bytes(existing)
    inode = destination.stat().st_ino
    import_legacy_file(source, destination)
    assert destination.read_bytes() == existing
    assert destination.stat().st_ino == inode
    assert source.read_bytes() == b"legacy"


def test_legacy_import_is_lazy_when_source_is_missing(tmp_path):
    destination = tmp_path / "nested" / "history.jsonl"
    import_legacy_file(tmp_path / "missing", destination)
    assert not destination.parent.exists()


def test_legacy_import_publishes_one_complete_concurrent_winner(tmp_path, monkeypatch):
    sources = [tmp_path / "first", tmp_path / "second"]
    for index, source in enumerate(sources):
        source.write_bytes(bytes([index]) * 100_000)
    destination = tmp_path / "nested" / "history.jsonl"
    barrier = Barrier(2)
    link = os.link

    def publish(source, target):
        barrier.wait(timeout=5)
        link(source, target)

    monkeypatch.setattr(os, "link", publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(import_legacy_file, path, destination) for path in sources
        ]
        for future in futures:
            future.result(timeout=5)
    assert destination.read_bytes() in [source.read_bytes() for source in sources]
    assert list(destination.parent.iterdir()) == [destination]


def test_legacy_import_failure_preserves_source_and_cleans_temporary(
    tmp_path, monkeypatch
):
    source = tmp_path / "old"
    source.write_bytes(b"legacy")
    destination = tmp_path / "nested" / "history.jsonl"

    def fail_link(_source, _target):
        raise OSError("publication failed")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(OSError, match="publication failed"):
        import_legacy_file(source, destination)
    assert source.read_bytes() == b"legacy"
    assert not destination.exists()
    assert not list(destination.parent.iterdir())
