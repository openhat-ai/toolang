from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from toolang.plugin.models.update import install_model_catalog


_DOWNLOADED_AT = datetime(2026, 8, 23, 10, 34, 55, tzinfo=timezone.utc)


def test_install_catalog_creates_immutable_version_and_single_relative_link(
    tmp_path: Path,
) -> None:
    content = _catalog_bytes("one")

    result = install_model_catalog(tmp_path, content, downloaded_at=_DOWNLOADED_AT)

    assert result.changed is True
    assert result.version.name.startswith("models-20260823T103455Z-")
    assert result.version.read_bytes() == content
    assert result.active.is_symlink()
    assert result.active.readlink() == Path("models") / result.version.name
    assert result.active.read_bytes() == content
    assert not (tmp_path / "models.json.prev").exists()


def test_install_catalog_same_sha_is_a_noop(tmp_path: Path) -> None:
    content = _catalog_bytes("one")
    first = install_model_catalog(tmp_path, content, downloaded_at=_DOWNLOADED_AT)

    second = install_model_catalog(
        tmp_path,
        content,
        downloaded_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )

    assert second.changed is False
    assert second.revision == first.revision
    assert len(tuple((tmp_path / "models").glob("models-*.json"))) == 1


def test_install_catalog_archives_valid_regular_active_file(tmp_path: Path) -> None:
    active = tmp_path / "models.json"
    active.write_bytes(_catalog_bytes("old"))

    result = install_model_catalog(
        tmp_path,
        _catalog_bytes("new"),
        downloaded_at=_DOWNLOADED_AT,
    )

    versions = tuple((tmp_path / "models").glob("models-*.json"))
    assert result.active.is_symlink()
    assert len(versions) == 2
    assert any(b'"old"' in path.read_bytes() for path in versions)
    assert any(b'"new"' in path.read_bytes() for path in versions)


def test_same_sha_regular_file_is_migrated_to_the_managed_link(tmp_path: Path) -> None:
    content = _catalog_bytes("one")
    active = tmp_path / "models.json"
    active.write_bytes(content)

    result = install_model_catalog(tmp_path, content, downloaded_at=_DOWNLOADED_AT)

    assert result.changed is True
    assert active.is_symlink()
    assert active.read_bytes() == content


def test_invalid_download_never_changes_active_catalog(tmp_path: Path) -> None:
    original = _catalog_bytes("old")
    active = tmp_path / "models.json"
    active.write_bytes(original)

    with pytest.raises(ValueError, match="invalid model catalog JSON"):
        install_model_catalog(tmp_path, b"{", downloaded_at=_DOWNLOADED_AT)

    assert active.read_bytes() == original
    assert not (tmp_path / "models").exists()


def _catalog_bytes(model_id: str) -> bytes:
    return json.dumps(
        {
            "test": {
                "id": "test",
                "name": "Test",
                "env": [],
                "npm": "@ai-sdk/openai-compatible",
                "models": {
                    model_id: {
                        "id": model_id,
                        "name": model_id,
                        "attachment": False,
                        "reasoning": False,
                        "tool_call": True,
                        "structured_output": True,
                        "temperature": True,
                        "release_date": "2026-01-01",
                        "last_updated": "2026-01-01",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "open_weights": False,
                        "limit": {"context": 1000, "output": 100},
                    }
                },
            }
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
