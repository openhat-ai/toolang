from __future__ import annotations

import os
from pathlib import Path

from toolang.state.source import (
    Source,
    is_source_path,
    scan_home_source,
    scan_source,
)


def test_source_tree_round_trips_nested_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    skill = source / "skills" / "pdf" / "scripts"
    skill.mkdir(parents=True)
    (source / "skills" / "pdf" / "SKILL.md").write_text("# PDF\n", encoding="utf-8")
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

    assert [item.name for item in source.root.children] == ["flows/research.too"]
    assert is_source_path(root, "alice", direct)
    assert not is_source_path(root, "alice", nested / "hidden.too")
    assert not is_source_path(root, "alice", root_flow)
