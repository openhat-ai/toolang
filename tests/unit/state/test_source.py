from __future__ import annotations

import os
from pathlib import Path

from toolang.state.source import Source, scan_source


def test_source_tree_round_trips_nested_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    skill = source / "skills" / "pdf" / "scripts"
    skill.mkdir(parents=True)
    (source / "skills" / "pdf" / "SKILL.md").write_text(
        "# PDF\n", encoding="utf-8"
    )
    (skill / "convert.py").write_text("pass\n", encoding="utf-8")

    tree = scan_source(source, ("skills", "config.toml"))
    snapshot = tmp_path / "source.json"
    tree.save(snapshot)

    assert Source.load(snapshot) == tree
    assert tree.root.children[0].name == "skills"
    assert tree.root.children[0].children[0].name == "pdf"


def test_source_tree_changes_when_nested_file_metadata_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    nested = source / "skills" / "pdf" / "scripts" / "convert.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("pass\n", encoding="utf-8")
    before = scan_source(source, ("skills",))

    nested.write_text("raise RuntimeError\n", encoding="utf-8")
    after = scan_source(source, ("skills",))

    assert after != before


def test_source_tree_is_intentionally_coarse_for_preserved_metadata(
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
    assert after == before


def test_source_tree_follows_symbolic_linked_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "roaming.too"
    source.mkdir()
    target.write_text("agent roaming\n", encoding="utf-8")
    (source / "agent.too").symlink_to(target)

    before = scan_source(source, ("agent.too",))
    target.write_text("agent changed\n", encoding="utf-8")
    after = scan_source(source, ("agent.too",))

    assert before.root.children[0].name == "agent.too"
    assert after != before
