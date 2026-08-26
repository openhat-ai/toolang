from __future__ import annotations

from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from dulwich import porcelain

import toolang.common.version as version


@pytest.fixture(autouse=True)
def clear_toolang_version_cache() -> Iterator[None]:
    version.toolang_version.cache_clear()
    yield
    version.toolang_version.cache_clear()


def test_base_toolang_version_reads_nearest_source_pyproject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "src" / "toolang" / "common" / "version.py"
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


def test_toolang_version_describes_development_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(version, "development_source", lambda: (True, tmp_path))
    monkeypatch.setattr(
        version,
        "embedded_source_version",
        lambda: pytest.fail("development source must ignore embedded build info"),
    )
    monkeypatch.setattr(
        version,
        "repository_source_version",
        lambda root: "v0.2.7-87-g3b492a92*" if root == tmp_path else None,
    )

    assert version.toolang_version() == "v0.2.7-87-g3b492a92*"


def test_toolang_version_reads_embedded_info_without_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_path = tmp_path / "toolang" / "common" / "version.py"
    module_path.parent.mkdir(parents=True)
    module_path.touch()
    (tmp_path / "toolang" / "_build_info.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "source_version": "v0.2.7-87-g3b492a92*",
                "revision": "3b492a92f1ed6282fc5b57a02d091339059cabcf",
                "dirty": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(version, "__file__", str(module_path))
    monkeypatch.setattr(version, "development_source", lambda: (False, None))
    monkeypatch.setattr(
        version,
        "repository_source_version",
        lambda *_args: pytest.fail("installed artifacts must not inspect a repository"),
    )

    assert version.toolang_version() == "v0.2.7-87-g3b492a92*"


@pytest.mark.parametrize(
    "payload",
    (
        None,
        {},
        {"schema": 2, "source_version": "v0.3.0"},
        {
            "schema": 1,
            "source_version": "v0.3.0",
            "revision": None,
            "dirty": "false",
        },
    ),
)
def test_embedded_source_version_rejects_missing_or_invalid_info(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    module_path = tmp_path / "toolang" / "common" / "version.py"
    module_path.parent.mkdir(parents=True)
    module_path.touch()
    if payload is not None:
        (tmp_path / "toolang" / "_build_info.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
    monkeypatch.setattr(version, "__file__", str(module_path))

    assert version.embedded_source_version() == "unknown"


def test_toolang_version_is_cached_once_per_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def describe(_root: Path) -> str:
        nonlocal calls
        calls += 1
        return "v0.3.0"

    monkeypatch.setattr(version, "development_source", lambda: (True, tmp_path))
    monkeypatch.setattr(version, "repository_source_version", describe)

    assert version.toolang_version() == "v0.3.0"
    assert version.toolang_version() == "v0.3.0"
    assert calls == 1


def test_repository_source_version_uses_tags_and_tracked_dirty_state(
    tmp_path: Path,
) -> None:
    porcelain.init(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    porcelain.add(tmp_path, tracked.name)
    porcelain.commit(
        tmp_path,
        message=b"Initial commit",
        author=b"Test Author <test@example.com>",
        committer=b"Test Author <test@example.com>",
    )
    porcelain.tag_create(
        tmp_path,
        "v0.3.0",
        author=b"Test Author <test@example.com>",
        message=b"Release v0.3.0",
        annotated=True,
    )

    assert version.repository_source_version(tmp_path) == "v0.3.0"

    (tmp_path / "untracked.txt").write_text("ignored\n", encoding="utf-8")
    assert version.repository_source_version(tmp_path) == "v0.3.0"

    tracked.write_text("dirty\n", encoding="utf-8")
    assert version.repository_source_version(tmp_path) == "v0.3.0*"


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


def test_local_file_url_preserves_literal_percent_escape(tmp_path: Path) -> None:
    source = tmp_path / "toolang%2Fsource"

    assert version._local_file_url(source.as_uri()) == source


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
