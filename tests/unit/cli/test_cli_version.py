from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from pathlib import Path

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
