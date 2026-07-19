from __future__ import annotations

from pathlib import Path

import pytest

from toolang.lang.ast import Program, to_data
from toolang.state.generation import (
    agent_state_version,
    load_current_version,
    load_generation_prepared,
    load_generation_resolved,
    load_generation_source,
    load_home_prepared,
    publish_current,
    prepared_generation_dir,
    prepared_version,
    write_generation,
)
from toolang.state.source import scan_source_tree


def test_prepared_version_includes_scope_source_and_resolution(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.toml").write_text("[caps]\n", encoding="utf-8")
    tree = scan_source_tree(source, ("config.toml",))
    resolved = {"schema": 1, "entries": []}

    root = prepared_version(
        scope="root",
        source=tree,
        resolved=resolved,
    )
    home = prepared_version(
        scope="home",
        source=tree,
        resolved=resolved,
    )
    upgraded = prepared_version(
        scope="root",
        source=tree,
        resolved=resolved,
    )
    refreshed = prepared_version(
        scope="root",
        source=tree,
        resolved={
            "schema": 1,
            "entries": [{"authored_ref": "repo@main", "resolved_ref": "repo@abc"}],
        },
    )

    assert len(root) == 32
    assert upgraded == root
    assert len({root, home, refreshed}) == 3


def test_agent_state_version_preserves_root_home_order(tmp_path: Path) -> None:
    root = bytes.fromhex("01" * 32)
    home = bytes.fromhex("02" * 32)

    version = agent_state_version(root, home)

    assert len(version) == 32
    assert version != agent_state_version(home, root)
    assert prepared_generation_dir(tmp_path, root) == (
        tmp_path / ".prepared" / "versions" / root.hex()
    )


def test_agent_state_version_rejects_non_sha256_values() -> None:
    with pytest.raises(ValueError, match="root version must contain 32 bytes"):
        agent_state_version(b"short", bytes(32))


def test_generation_is_self_contained_and_can_be_published(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "agent.too").write_text("agent alice\n", encoding="utf-8")
    source = scan_source_tree(source_dir, ("agent.too",))
    resolved = {"schema": 1, "entries": []}
    prepared = {
        "schema": 1,
        "scope": "home",
        "toolang_version": "0.2.7",
        "config": {"models": {"default": "fast"}},
        "program": to_data(Program.from_source("agic hello:\n  Hello.\n")),
        "caps": [],
    }

    version = write_generation(
        toolang_root=tmp_path,
        agent_name="alice",
        scope="home",
        source=source,
        resolved=resolved,
        prepared=prepared,
        files={"agent.too": b"agent alice\n", "authored/prompts/review.md": b"# Review\n"},
    )
    generation = prepared_generation_dir(tmp_path, version, "alice")
    publish_current(tmp_path, version, "alice")

    assert load_current_version(tmp_path, "alice") == version
    assert load_generation_source(generation) == source
    assert load_generation_resolved(generation) == resolved
    assert load_generation_prepared(generation) == prepared
    home = load_home_prepared(tmp_path, "alice")
    assert home.version == version
    assert home.config == {"models": {"default": "fast"}}
    assert home.program.find_agic("hello") is not None
    assert (generation / "files" / "agent.too").read_bytes() == b"agent alice\n"
    assert (
        generation / "files" / "authored" / "prompts" / "review.md"
    ).read_bytes() == b"# Review\n"


def test_home_generation_loads_program_without_reparsing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_text = "agic hello:\n  Hello.\n"
    (source_dir / "agent.too").write_text(source_text, encoding="utf-8")
    source = scan_source_tree(source_dir, ("agent.too",))
    version = write_generation(
        toolang_root=tmp_path,
        agent_name="alice",
        scope="home",
        source=source,
        resolved={"schema": 1, "entries": []},
        prepared={
            "schema": 1,
            "scope": "home",
            "toolang_version": "0.2.7",
            "program": to_data(Program.from_source(source_text)),
            "caps": [],
        },
        files={"agent.too": source_text.encode()},
    )
    publish_current(tmp_path, version, "alice")

    def fail_parse(_cls: type[Program], _source: str) -> Program:
        raise AssertionError("prepared program must not be reparsed")

    monkeypatch.setattr(Program, "from_source", classmethod(fail_parse))

    assert load_home_prepared(tmp_path, "alice").program.find_agic("hello")


def test_generation_rejects_files_outside_its_files_directory(
    tmp_path: Path,
) -> None:
    source = scan_source_tree(tmp_path, ())

    with pytest.raises(ValueError, match="generation file path must be relative"):
        write_generation(
            toolang_root=tmp_path,
            scope="root",
            source=source,
            resolved={"schema": 1, "entries": []},
            prepared={
                "schema": 1,
                "scope": "root",
                "toolang_version": "0.2.7",
                "caps": [],
            },
            files={"../escape": b"bad"},
        )
