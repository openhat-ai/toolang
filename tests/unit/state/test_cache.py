from __future__ import annotations

from pathlib import Path

import pytest

from toolang.common.layout import AgentLayout
from toolang.lang.ast import Program, to_data
from toolang.state.state import CapResolution, agent_state_version
from toolang.state.cache import (
    load_current_version,
    load_version_prepared,
    load_version_resolved,
    load_version_source,
    load_home_prepared,
    publish_current,
    prepared_version_dir,
    prepared_version,
    write_prepared,
)
from toolang.state.source import scan_source


def test_prepared_version_includes_scope_source_and_resolution(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.toml").write_text("[caps]\n", encoding="utf-8")
    tree = scan_source(source, ("config.toml",))
    resolutions: tuple[CapResolution, ...] = ()

    root = prepared_version(
        scope="root",
        source=tree,
        resolutions=resolutions,
    )
    home = prepared_version(
        scope="home",
        source=tree,
        resolutions=resolutions,
    )
    upgraded = prepared_version(
        scope="root",
        source=tree,
        resolutions=resolutions,
    )
    refreshed = prepared_version(
        scope="root",
        source=tree,
        resolutions=(
            CapResolution(
                kind="prompt",
                name="rewrite",
                form="wired",
                authored_ref="repo@main",
                resolved_ref="repo@abc",
                definition="config.toml",
                materialized="files/wired/prompts/rewrite.md",
                content_hash="00" * 32,
                files=(),
            ),
        ),
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
    layout = AgentLayout.resident(tmp_path, "alice")
    assert prepared_version_dir(layout, "root", root) == (
        tmp_path / ".state" / "versions" / root.hex()
    )


def test_agent_state_version_rejects_non_sha256_values() -> None:
    with pytest.raises(ValueError, match="root version must contain 32 bytes"):
        agent_state_version(b"short", bytes(32))


def test_prepared_version_is_self_contained_and_can_be_published(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "agent.too").write_text("agent alice\n", encoding="utf-8")
    source = scan_source(source_dir, ("agent.too",))
    prepared = {
        "schema": 1,
        "scope": "home",
        "toolang_version": "0.2.7",
        "config": {"models": {"default": "fast"}},
        "program": to_data(Program.from_source("agic hello:\n  Hello.\n")),
        "caps": [],
    }

    version = write_prepared(
        layout=AgentLayout.resident(tmp_path, "alice"),
        scope="home",
        source=source,
        resolutions=(),
        prepared=prepared,
        files={
            "agent.too": b"agent alice\n",
            "authored/prompts/review.md": b"# Review\n",
        },
    )
    layout = AgentLayout.resident(tmp_path, "alice")
    version_dir = prepared_version_dir(layout, "home", version)
    publish_current(layout, "home", version)

    assert load_current_version(layout, "home") == version
    assert load_version_source(version_dir) == source
    assert load_version_resolved(version_dir) == {"schema": 1, "entries": []}
    assert load_version_prepared(version_dir) == prepared
    home = load_home_prepared(layout)
    assert home.version == version
    assert home.config == {"models": {"default": "fast"}}
    assert home.program.find_agic("hello") is not None
    assert (version_dir / "files" / "agent.too").read_bytes() == b"agent alice\n"
    assert (
        version_dir / "files" / "authored" / "prompts" / "review.md"
    ).read_bytes() == b"# Review\n"


def test_home_prepared_loads_program_without_reparsing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_text = "agic hello:\n  Hello.\n"
    (source_dir / "agent.too").write_text(source_text, encoding="utf-8")
    source = scan_source(source_dir, ("agent.too",))
    version = write_prepared(
        layout=AgentLayout.resident(tmp_path, "alice"),
        scope="home",
        source=source,
        resolutions=(),
        prepared={
            "schema": 1,
            "scope": "home",
            "toolang_version": "0.2.7",
            "program": to_data(Program.from_source(source_text)),
            "caps": [],
        },
        files={"agent.too": source_text.encode()},
    )
    layout = AgentLayout.resident(tmp_path, "alice")
    publish_current(layout, "home", version)

    def fail_parse(_cls: type[Program], _source: str) -> Program:
        raise AssertionError("prepared program must not be reparsed")

    monkeypatch.setattr(Program, "from_source", classmethod(fail_parse))

    assert load_home_prepared(layout).program.find_agic("hello")


def test_prepared_version_rejects_files_outside_its_files_directory(
    tmp_path: Path,
) -> None:
    source = scan_source(tmp_path, ())

    with pytest.raises(ValueError, match="cache file path must be relative"):
        write_prepared(
            layout=AgentLayout.resident(tmp_path, "alice"),
            scope="root",
            source=source,
            resolutions=(),
            prepared={
                "schema": 1,
                "scope": "root",
                "toolang_version": "0.2.7",
                "caps": [],
            },
            files={"../escape": b"bad"},
        )
