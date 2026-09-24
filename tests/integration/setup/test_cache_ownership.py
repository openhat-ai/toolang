"""Offline Linux permission checks; run as root to exercise distinct UIDs."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest


@pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0,
    reason="requires Linux root for isolated subprocess UID changes",
)
@pytest.mark.parametrize("kind", ["listing", "source"])
@pytest.mark.parametrize("first_writer", ["host", "guest"])
def test_cache_remains_owned_and_usable_by_host(kind: str, first_writer: str) -> None:
    result = subprocess.run(
        [sys.executable, __file__, kind, first_writer],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _check_ownership(kind: str, first_writer: str) -> None:
    from toolang.plugin.catalogs.models_dev.parsing import (
        model_catalog_snapshot_from_data,
    )
    from toolang.setup.cache import ModelCatalogCache
    from toolang.setup.records import CatalogRecords, ModelListingCache

    with tempfile.TemporaryDirectory(prefix="toolang-cache-owner-") as temporary:
        root = Path(temporary)
        os.chown(root, 1000, 1000)
        directory = root / ".setup" / "models"
        listing = ModelListingCache(directory)
        source = ModelCatalogCache(directory / "sources")
        records = CatalogRecords(providers=(), models=())
        snapshot = model_catalog_snapshot_from_data({}, revision="test")

        def store() -> None:
            if kind == "listing":
                assert listing.store(
                    records, inputs={}, environment_names=(), environ={}
                )
            else:
                source.store_source("test", revision="test", snapshot=snapshot)

        def check() -> None:
            if kind == "listing":
                assert listing.load(inputs={}, environ={}) == records
            else:
                assert source.load_source("test", revision="test") == snapshot

        old_mask = os.umask(0o077)
        old_gid = os.getegid()
        try:
            if first_writer == "host":
                os.setegid(1000)
                os.seteuid(1000)
                store()
                check()
                os.seteuid(0)
                os.setegid(old_gid)
            store()
            for path in root.rglob("*"):
                stat = path.stat()
                assert (stat.st_uid, stat.st_gid) == (1000, 1000), path
                assert stat.st_mode & 0o077 == 0, path
            os.setegid(1000)
            os.seteuid(1000)
            check()
            store()  # The host must also be able to reopen the shared lock.
            check()
        finally:
            os.seteuid(0)
            os.setegid(old_gid)
            os.umask(old_mask)


if __name__ == "__main__":
    _check_ownership(*sys.argv[1:])
