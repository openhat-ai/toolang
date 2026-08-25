from __future__ import annotations

from importlib.metadata import PackageNotFoundError
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import toolang.cli.common.version as version


def test_base_toolang_version_reads_nearest_source_pyproject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "src" / "toolang" / "cli" / "common" / "version.py"
    source.parent.mkdir(parents=True)
    source.touch()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "toolang"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(version, "__file__", str(source))
    monkeypatch.setattr(
        version,
        "package_version",
        lambda _name: (_ for _ in ()).throw(PackageNotFoundError()),
    )

    assert version.base_toolang_version() == "1.2.3"


@pytest.mark.parametrize(
    ("sha", "status", "suffix"),
    (("abc123", "", "+abc123"), ("abc123", " M file", "+abc123*"), (None, "", "")),
)
def test_source_state_suffix_reports_revision_and_dirty_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sha: str | None,
    status: str,
    suffix: str,
) -> None:
    monkeypatch.setattr(version, "source_tree_root", lambda: tmp_path)
    monkeypatch.setattr(
        version,
        "git_output",
        lambda _root, command, *_args: sha if command == "rev-parse" else status,
    )

    assert version.source_state_suffix() == suffix


def test_development_source_reads_editable_distribution_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "toolang source"
    source.mkdir()
    direct_url = json.dumps(
        {
            "url": source.as_uri(),
            "dir_info": {"editable": True},
        }
    )
    distribution = SimpleNamespace(read_text=lambda _name: direct_url)
    monkeypatch.setattr(version, "package_distribution", lambda _name: distribution)

    assert version.development_source() == (True, source)


def test_development_source_ignores_non_editable_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_url = json.dumps(
        {
            "url": "file:///tmp/toolang",
            "dir_info": {"editable": False},
        }
    )
    distribution = SimpleNamespace(read_text=lambda _name: direct_url)
    monkeypatch.setattr(version, "package_distribution", lambda _name: distribution)

    assert version.development_source() == (False, None)


def test_development_source_keeps_editable_signal_without_safe_local_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_url = json.dumps(
        {
            "url": "https://example.test/toolang",
            "dir_info": {"editable": True},
        }
    )
    distribution = SimpleNamespace(read_text=lambda _name: direct_url)
    monkeypatch.setattr(version, "package_distribution", lambda _name: distribution)

    assert version.development_source() == (True, None)


def test_development_source_falls_back_to_source_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        version,
        "package_distribution",
        lambda _name: (_ for _ in ()).throw(PackageNotFoundError()),
    )
    monkeypatch.setattr(version, "source_project_root", lambda: tmp_path)

    assert version.development_source() == (True, tmp_path)
