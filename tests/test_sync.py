from __future__ import annotations

from pathlib import Path

import pytest

from toolang.agent_refs import resolve_agent_ref
from toolang.errors import ToolangError
from toolang.files import SyncState, SyncedProgram
from toolang.layout import (
    agent_program_path,
    agent_sync_state_path,
    resolve_toolang_root,
    synced_caps_root,
    toolang_config_path,
)
from toolang.parser import parse_program
from toolang.sync import ensure_agent_synced, sync_agent

FIXTURE = Path(__file__).parent / "fixtures" / "sample.too"


def test_synced_program_round_trip(tmp_path) -> None:
    path = tmp_path / "program.json"
    program = parse_program(FIXTURE.read_text(encoding="utf-8"))
    synced_program = SyncedProgram.from_program(program)

    synced_program.save(path)
    loaded = SyncedProgram.load(path)

    assert loaded == synced_program
    assert loaded.to_program().to_dict() == program.to_dict()


def test_sync_agent_writes_program_and_source_caps(tmp_path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    source_path = home / "alice.too"
    source_path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    synced_program = sync_agent(agent)

    assert agent_program_path(home, "alice").exists()
    assert agent_sync_state_path(home, "alice").exists()
    assert (synced_caps_root(home) / "prompts" / "summarize.json").exists()
    assert synced_program.to_program().to_dict() == parse_program(source_path.read_text()).to_dict()


def test_ensure_agent_synced_reuses_fresh_state(tmp_path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    sync_agent(agent)
    before = agent_sync_state_path(home, "alice").read_text(encoding="utf-8")

    ensure_agent_synced(agent)
    after = agent_sync_state_path(home, "alice").read_text(encoding="utf-8")

    assert after == before


def test_ensure_agent_synced_rebuilds_missing_synced_caps(tmp_path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    sync_agent(agent)
    prompt_path = synced_caps_root(home) / "prompts" / "summarize.json"
    prompt_path.unlink()

    ensure_agent_synced(agent)

    assert prompt_path.exists()


def test_sync_agent_rejects_managed_caps_until_resolution_exists(tmp_path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    toolang_config_path(home).write_text(
        """
[skills]
"pdf-processing" = { ref = "briceyan/pdf-processing" }
""".strip(),
        encoding="utf-8",
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)

    with pytest.raises(ToolangError, match="Managed caps"):
        sync_agent(agent)
