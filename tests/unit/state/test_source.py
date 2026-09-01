from __future__ import annotations

import os
from pathlib import Path
import shutil

import pytest

from toolang.state.source import (
    SOURCE_SCHEMA,
    SourceManifest,
    build_source_manifest,
    is_source_path,
    observe_source,
    scan_home_source,
    scan_source,
)


def test_source_manifest_round_trips_nested_content(tmp_path: Path) -> None:
    source = tmp_path / "source"
    skill = source / "skills" / "pdf" / "scripts"
    skill.mkdir(parents=True)
    (source / "skills" / "pdf" / "SKILL.md").write_text("# PDF\n", encoding="utf-8")
    (skill / "convert.py").write_text("pass\n", encoding="utf-8")

    manifest = scan_source(source, ("skills", "config.toml"))
    loaded = SourceManifest.from_data(manifest.to_data())

    assert loaded == manifest
    assert [item.path for item in manifest.files] == [
        "skills/pdf/SKILL.md",
        "skills/pdf/scripts/convert.py",
    ]


def test_source_manifest_changes_when_nested_file_content_changes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    nested = source / "skills" / "pdf" / "scripts" / "convert.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("pass\n", encoding="utf-8")
    before = scan_source(source, ("skills",))

    nested.write_text("raise RuntimeError\n", encoding="utf-8")
    after = scan_source(source, ("skills",))

    assert after != before


def test_source_manifest_keeps_empty_non_config_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.too").touch()

    manifest = scan_source(source, ("agent.too",), project_configs=True)

    assert [(item.path, item.size) for item in manifest.files] == [("agent.too", 0)]


def test_source_manifest_detects_content_change_with_preserved_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    program = source / "agent.too"
    program.write_bytes(b"agent alice\n")
    original = program.stat()
    before = scan_source(source, ("agent.too",))

    program.write_bytes(b"agent other\n")
    os.utime(program, ns=(original.st_atime_ns, original.st_mtime_ns))
    after = scan_source(source, ("agent.too",))

    assert program.stat().st_size == original.st_size
    assert after != before


def test_source_manifest_ignores_metadata_only_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    program = source / "agent.too"
    program.write_text("agent alice\n", encoding="utf-8")
    before = scan_source(source, ("agent.too",))

    changed = program.stat().st_mtime_ns + 1_000_000_000
    os.utime(program, ns=(changed, changed))
    after = scan_source(source, ("agent.too",))

    assert after == before


def test_source_manifest_follows_symbolic_linked_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "roaming.too"
    source.mkdir()
    target.write_text("agent roaming\n", encoding="utf-8")
    (source / "agent.too").symlink_to(target)

    before = scan_source(source, ("agent.too",))
    target.write_text("agent changed\n", encoding="utf-8")
    after = scan_source(source, ("agent.too",))

    assert before.files[0].path == "agent.too"
    assert after != before


def test_source_manifest_is_portable_across_absolute_roots(tmp_path: Path) -> None:
    host = tmp_path / "host"
    guest = tmp_path / "guest"
    (host / "skills" / "pdf").mkdir(parents=True)
    (host / "skills" / "pdf" / "SKILL.md").write_text("# PDF\n", encoding="utf-8")
    shutil.copytree(host, guest)

    assert scan_source(host, ("skills",)) == scan_source(guest, ("skills",))


@pytest.mark.parametrize(
    "data, message",
    (
        (
            {
                "files": [{"path": "../escape", "sha256": "0" * 64, "size": 1}],
                "schema": SOURCE_SCHEMA,
            },
            "portable and relative",
        ),
        (
            {
                "files": [{"path": "agent.too", "sha256": "invalid", "size": 1}],
                "schema": SOURCE_SCHEMA,
            },
            "SHA-256",
        ),
        (
            {
                "files": [
                    {"path": "b", "sha256": "0" * 64, "size": 1},
                    {"path": "a", "sha256": "0" * 64, "size": 1},
                ],
                "schema": SOURCE_SCHEMA,
            },
            "sorted and unique",
        ),
    ),
)
def test_source_manifest_rejects_unsafe_or_noncanonical_data(
    data: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        SourceManifest.from_data(data)


def test_source_manifest_rejects_symbolic_link_directories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "external"
    source.mkdir()
    target.mkdir()
    (source / "skills").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic-link directories"):
        scan_source(source, ("skills",))


def test_manifest_builder_reuses_only_unchanged_file_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    one = source / "one.txt"
    two = source / "two.txt"
    one.write_text("one", encoding="utf-8")
    two.write_text("two", encoding="utf-8")
    before_observation = observe_source(source, ("one.txt", "two.txt"))
    before = build_source_manifest(before_observation, project_configs=False)
    two.write_text("changed", encoding="utf-8")
    after_observation = observe_source(source, ("one.txt", "two.txt"))
    reads: list[Path] = []
    original_read = Path.read_bytes

    def count_read(path: Path) -> bytes:
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", count_read)

    after = build_source_manifest(
        after_observation,
        project_configs=False,
        previous_observation=before_observation,
        previous_manifest=before,
    )

    assert reads == [two]
    assert after != before


def test_home_source_discovers_only_direct_lowercase_too_flows(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    flows = root / "agents" / "alice" / "flows"
    nested = flows / "nested"
    nested.mkdir(parents=True)
    direct = flows / "research.too"
    direct.write_text("flow:\n  pass\n", encoding="utf-8")
    (flows / "notes.txt").write_text("ignored", encoding="utf-8")
    (flows / "upper.TOO").write_text("flow:\n  pass\n", encoding="utf-8")
    (nested / "hidden.too").write_text("flow:\n  pass\n", encoding="utf-8")
    root_flow = root / "flows" / "shared.too"
    root_flow.parent.mkdir()
    root_flow.write_text("flow:\n  pass\n", encoding="utf-8")

    source = scan_home_source(root, "alice")

    assert [item.path for item in source.files] == ["flows/research.too"]
    assert is_source_path(root, "alice", direct)
    assert not is_source_path(root, "alice", nested / "hidden.too")
    assert not is_source_path(root, "alice", root_flow)
