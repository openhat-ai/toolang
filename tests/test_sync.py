from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from toolang.agent_refs import resolve_agent_ref
from toolang.errors import ToolangError
from toolang.files import AgentLock, SyncState, SyncedProgram
from toolang.layout import (
    agent_lock_path,
    agent_program_path,
    agent_sync_state_path,
    resolve_toolang_root,
    synced_caps_root,
    toolang_config_path,
)
from toolang.parser import parse_program
from toolang.sync import ensure_agent_synced, sync_agent
from toolang_caps import ResolvedCapRef

PARSE_FIXTURE = Path(__file__).parent / "fixtures" / "sample.too"
SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"
REMOTE_SKILL_FIXTURE = Path(__file__).parent / "fixtures" / "remote-skill" / "pdf-processing"


def test_synced_program_round_trip(tmp_path) -> None:
    path = tmp_path / "program.json"
    program = parse_program(PARSE_FIXTURE.read_text(encoding="utf-8"))
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
    source_path.write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    synced_program = sync_agent(agent)

    assert agent_program_path(home, "alice").exists()
    assert agent_sync_state_path(home, "alice").exists()
    assert (synced_caps_root(home) / "prompt" / "summarize.md").exists()
    assert (synced_caps_root(home) / "prompt" / "summarize.meta.json").exists()
    assert (synced_caps_root(home) / "service" / "github.md").read_text(encoding="utf-8").startswith("---\n")
    service_meta = (synced_caps_root(home) / "service" / "github.meta.json").read_text(encoding="utf-8")
    assert '"transport": "http"' in service_meta
    assert synced_program.to_program().to_dict() == parse_program(source_path.read_text()).to_dict()


def test_sync_agent_materializes_used_skills_and_writes_agent_lock(tmp_path, monkeypatch) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        """
use skill by3gus/pdf-processing

thunk summarize:
    Summarize the selected PDFs.
""".strip(),
        encoding="utf-8",
    )

    def fake_resolve(ref: str) -> ResolvedCapRef:
        return ResolvedCapRef(
            kind="skill",
            name="pdf-processing",
            ref=ref,
            repo="by3gus/agent-skills",
            path="skills/pdf-processing",
            rev="abc123",
        )

    def fake_fetch(resolved: ResolvedCapRef):
        fetched_root = tmp_path / "fetched" / resolved.name
        shutil.copytree(REMOTE_SKILL_FIXTURE, fetched_root)
        files = sorted(
            str(path.relative_to(fetched_root))
            for path in fetched_root.rglob("*")
            if path.is_file()
        )
        return fetched_root, files

    monkeypatch.setattr("toolang.sync.resolve_github_skill_ref", fake_resolve)
    monkeypatch.setattr("toolang.sync.fetch_github_tree", fake_fetch)

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    sync_agent(agent)

    skill_root = synced_caps_root(home) / "skill" / "pdf-processing"
    assert (skill_root / "SKILL.md").read_text(encoding="utf-8") == (
        REMOTE_SKILL_FIXTURE / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert (skill_root / "assets" / "template.md").read_text(encoding="utf-8") == (
        REMOTE_SKILL_FIXTURE / "assets" / "template.md"
    ).read_text(encoding="utf-8")

    lock = AgentLock.load(agent_lock_path(home))
    assert lock.skills["pdf-processing"].repo == "by3gus/agent-skills"
    assert lock.skills["pdf-processing"].path == "skills/pdf-processing"
    assert lock.skills["pdf-processing"].rev == "abc123"


def test_ensure_agent_synced_reuses_fresh_state(tmp_path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

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
    (home / "alice.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    sync_agent(agent)
    prompt_path = synced_caps_root(home) / "prompt" / "summarize.md"
    prompt_path.unlink()

    ensure_agent_synced(agent)

    assert prompt_path.exists()


def test_sync_agent_rejects_managed_caps_until_resolution_exists(tmp_path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
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
