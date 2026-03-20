from __future__ import annotations

import shutil
from pathlib import Path

from toolang.agent_refs import resolve_agent_ref
from toolang.files.program import SyncedProgram
from toolang.files.sync_state import SyncState
from toolang.layout import (
    agent_synced_caps_root,
    agent_sync_path,
    global_caps_dir,
    global_source_path,
    global_synced_caps_root,
    resolve_toolang_root,
    shared_caps_dir,
    shared_source_path,
    synced_caps_root,
)
from toolang.parser import parse_program
from toolang.sync import ensure_agent_synced, sync_agent
from toolang_caps.models import ResolvedCapRef

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

    assert agent_sync_path(home, "alice") == synced_caps_root(home) / "alice.state.json"
    assert agent_sync_path(home, "alice").exists()
    assert not (synced_caps_root(home) / "agents").exists()
    assert (synced_caps_root(home) / "prompts" / "summarize.md").exists()
    assert (synced_caps_root(home) / "prompts" / "summarize.meta.json").exists()
    assert (synced_caps_root(home) / "services" / "github.md").read_text(encoding="utf-8").startswith("---\n")
    service_meta = (synced_caps_root(home) / "services" / "github.meta.json").read_text(encoding="utf-8")
    assert '"transport": "http"' in service_meta
    assert synced_program.to_program().to_dict() == parse_program(source_path.read_text()).to_dict()


def test_sync_agent_materializes_used_skills_and_writes_agent_refs_into_state(
    tmp_path, monkeypatch
) -> None:
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
    (home / "bob.too").write_text(
        """
use skill by3gus/pdf-processing

thunk review:
    Review the selected PDFs.
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
        fetched_root = tmp_path / "fetched" / "materialized" / resolved.name
        fetched_root.parent.mkdir(parents=True, exist_ok=True)
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

    skill_root = agent_synced_caps_root(home, "alice") / "skills" / "pdf-processing"
    assert (skill_root / "SKILL.md").read_text(encoding="utf-8") == (
        REMOTE_SKILL_FIXTURE / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert (skill_root / "assets" / "template.md").read_text(encoding="utf-8") == (
        REMOTE_SKILL_FIXTURE / "assets" / "template.md"
    ).read_text(encoding="utf-8")

    alice_state = SyncState.load(agent_sync_path(home, "alice"))
    bob_state = SyncState.load(agent_sync_path(home, "bob"))
    assert alice_state.agent_refs.skills["pdf-processing"].repo == "by3gus/agent-skills"
    assert bob_state.agent_refs.skills["pdf-processing"].path == "skills/pdf-processing"
    assert alice_state.agent_refs.skills["pdf-processing"].rev == "abc123"
    assert not (home / "toolang.lock").exists()
    assert not (synced_caps_root(home) / "skills" / "pdf-processing").exists()
    assert (agent_synced_caps_root(home, "bob") / "skills" / "pdf-processing" / "SKILL.md").exists()


def test_ensure_agent_synced_reuses_fresh_state(tmp_path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    sync_agent(agent)
    before = agent_sync_path(home, "alice").read_text(encoding="utf-8")

    ensure_agent_synced(agent)
    after = agent_sync_path(home, "alice").read_text(encoding="utf-8")

    assert after == before


def test_ensure_agent_synced_rebuilds_missing_synced_caps(tmp_path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    sync_agent(agent)
    prompt_path = synced_caps_root(home) / "prompts" / "summarize.md"
    prompt_path.unlink()

    ensure_agent_synced(agent)

    assert prompt_path.exists()


def test_ensure_agent_synced_rebuilds_when_a_sibling_agent_changes(tmp_path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (home / "bob.too").write_text(
        """
thunk review:
    Review the change set.
""".strip(),
        encoding="utf-8",
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    sync_agent(agent)
    before = agent_sync_path(home, "alice").read_text(encoding="utf-8")

    (home / "bob.too").write_text(
        """
thunk review:
    Review the change set carefully.
""".strip(),
        encoding="utf-8",
    )

    ensure_agent_synced(agent)
    after = agent_sync_path(home, "alice").read_text(encoding="utf-8")

    assert after != before


def test_sync_agent_reads_shared_and_global_skill_sources_without_collapsing_names(
    tmp_path, monkeypatch
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        """
use skill by3hak/repo-search

thunk review:
    Review the change set.
""".strip(),
        encoding="utf-8",
    )
    shared_source_path(home).write_text("use skill by3gus/repo-search\n", encoding="utf-8")
    global_source_path(root).write_text("use skill by3hak/repo-search\n", encoding="utf-8")

    def fake_resolve(ref: str) -> ResolvedCapRef:
        owner, _, name = ref.partition("/")
        repo_name = "agent-skills" if owner == "by3gus" else "skills"
        path = f"skills/{name}" if repo_name == "agent-skills" else name
        return ResolvedCapRef(
            kind="skill",
            name=name,
            ref=ref,
            repo=f"{owner}/{repo_name}",
            path=path,
            rev=f"rev-{owner}",
        )

    def fake_fetch(resolved: ResolvedCapRef):
        fetched_root = tmp_path / "fetched" / resolved.repo.replace("/", "__") / resolved.name
        fetched_root.parent.mkdir(parents=True, exist_ok=True)
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

    state = SyncState.load(agent_sync_path(home, "alice"))
    assert state.global_refs.skills["repo-search"].ref == "by3hak/repo-search"
    assert state.shared_refs.skills["repo-search"].ref == "by3gus/repo-search"
    assert state.agent_refs.skills["repo-search"].ref == "by3hak/repo-search"
    assert (global_synced_caps_root(root) / "skills" / "repo-search" / "SKILL.md").exists()
    assert (synced_caps_root(home) / "skills" / "repo-search" / "SKILL.md").exists()
    assert (agent_synced_caps_root(home, "alice") / "skills" / "repo-search" / "SKILL.md").exists()


def test_sync_agent_materializes_shared_and_global_local_skills(tmp_path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        """
thunk review:
    Review the change set.
""".strip(),
        encoding="utf-8",
    )

    shared_skill = shared_caps_dir(home, "skill") / "local-shared"
    shared_skill.mkdir(parents=True)
    (shared_skill / "SKILL.md").write_text("# Shared\n", encoding="utf-8")

    global_skill = global_caps_dir(root, "skill") / "local-global"
    global_skill.mkdir(parents=True)
    (global_skill / "SKILL.md").write_text("# Global\n", encoding="utf-8")

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    sync_agent(agent)

    state = SyncState.load(agent_sync_path(home, "alice"))
    assert state.shared_refs.skills["local-shared"].path == "skills/local-shared"
    assert state.shared_refs.skills["local-shared"].ref is None
    assert state.global_refs.skills["local-global"].path == "skills/local-global"
    assert (synced_caps_root(home) / "skills" / "local-shared" / "SKILL.md").exists()
    assert (global_synced_caps_root(root) / "skills" / "local-global" / "SKILL.md").exists()
