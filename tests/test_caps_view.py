from __future__ import annotations

from pathlib import Path
from typing import cast

from toolang.agent.prepared import prepare_agent
from toolang.agent.refs import resolve_agent_ref
from toolang.caps import CapScopeSelection, load_prepared_caps
from toolang.layout import global_caps_dir
from toolang.layout import resolve_toolang_root
from toolang.layout import global_source_path, shared_caps_dir, shared_source_path
from toolang.concepts.caps import CapKind, CapRef, ServiceFrontmatter

SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"
REMOTE_SKILL_FIXTURE = Path(__file__).parent / "fixtures" / "remote-skill" / "pdf-processing"
REMOTE_SERVICE_FIXTURE = Path(__file__).parent / "fixtures" / "remote-service" / "github.md"
REMOTE_PROMPT_FIXTURE = Path(__file__).parent / "fixtures" / "remote-prompt" / "rewrite.md"
REMOTE_PSYCHE_FIXTURE = Path(__file__).parent / "fixtures" / "remote-psyche" / "reviewer.md"


def test_load_prepared_caps_is_scoped_to_current_agent(tmp_path: Path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (home / "bob.too").write_text(
        """
service jira: ```md
Use this service for Jira.
```

prompt triage: ```md
Triage the issue.
```

thunk review:
    Review the issue.
""".strip(),
        encoding="utf-8",
    )

    prepared = prepare_agent(resolve_agent_ref("team/alice", cwd=tmp_path, toolang_root=root))
    caps = load_prepared_caps(prepared)

    assert [item.name for item in caps.services] == ["github"]
    assert [item.name for item in caps.prompts] == ["summarize"]
    assert [item.name for item in caps.psyches] == ["reviewer"]


def test_load_prepared_caps_overlays_skill_scopes_at_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        """
use skill by3hak/repo-search

thunk review:
    Review the issue.
""".strip(),
        encoding="utf-8",
    )
    shared_source_path(home).write_text("use skill by3gus/repo-search\n", encoding="utf-8")
    global_source_path(root).write_text("use skill by3hak/repo-search\n", encoding="utf-8")

    def fake_resolve(kind: str, ref: str) -> CapRef:
        typed_kind = cast(CapKind, kind)
        owner, _, name = ref.partition("/")
        assert typed_kind == "skill"
        repo_name = "agent-skills" if owner == "by3gus" else "skills"
        path = f"skills/{name}" if repo_name == "agent-skills" else name
        return CapRef(
            kind=typed_kind,
            name=name,
            ref=ref,
            repo=f"{owner}/{repo_name}",
            path=path,
            rev=f"rev-{owner}",
        )

    def fake_fetch(resolved: CapRef):
        fetched_root = tmp_path / "fetched" / resolved.repo.replace("/", "__") / resolved.name
        fetched_root.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copytree(REMOTE_SKILL_FIXTURE, fetched_root)
        files = sorted(
            str(path.relative_to(fetched_root))
            for path in fetched_root.rglob("*")
            if path.is_file()
        )
        return fetched_root, files

    monkeypatch.setattr("toolang.caps.github.resolve_github_cap_ref", fake_resolve)
    monkeypatch.setattr("toolang.caps.github.fetch_github_artifact", fake_fetch)

    prepared = prepare_agent(resolve_agent_ref("team/alice", cwd=tmp_path, toolang_root=root))
    caps = load_prepared_caps(prepared)

    assert [item.name for item in caps.skills] == ["repo-search"]
    assert caps.skills[0].ref == "by3hak/repo-search"


def test_prepare_agent_overlays_text_cap_scopes_at_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        """
use service by3hak/github

prompt summarize: ```md
Agent summary prompt.

{{input}}
```

thunk review(user):
    Review the issue.
""".strip(),
        encoding="utf-8",
    )
    shared_source_path(home).write_text("use prompt by3gus/rewrite\n", encoding="utf-8")
    global_source_path(root).write_text("use psyche by3hak/reviewer\n", encoding="utf-8")
    (shared_caps_dir(home, "service") / "github.md").parent.mkdir(parents=True)
    (shared_caps_dir(home, "service") / "github.md").write_text(
        REMOTE_SERVICE_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (global_caps_dir(root, "prompt") / "rewrite.md").parent.mkdir(parents=True)
    (global_caps_dir(root, "prompt") / "rewrite.md").write_text(
        REMOTE_PROMPT_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    def fake_resolve(kind: str, ref: str) -> CapRef:
        typed_kind = cast(CapKind, kind)
        owner, _, name = ref.partition("/")
        repo_name = {
            "service": "agent-services" if owner == "by3gus" else "services",
            "prompt": "agent-prompts" if owner == "by3gus" else "prompts",
            "psyche": "agent-psyches" if owner == "by3gus" else "psyches",
        }[typed_kind]
        path = {
            "service": f"services/{name}.md" if repo_name == "agent-services" else f"{name}.md",
            "prompt": f"prompts/{name}.md" if repo_name == "agent-prompts" else f"{name}.md",
            "psyche": f"psyches/{name}.md" if repo_name == "agent-psyches" else f"{name}.md",
        }[typed_kind]
        return CapRef(
            kind=typed_kind,
            name=name,
            ref=ref,
            repo=f"{owner}/{repo_name}",
            path=path,
            rev=f"rev-{owner}",
        )

    def fake_fetch(resolved: CapRef):
        import shutil

        fixture = {
            "service": REMOTE_SERVICE_FIXTURE,
            "prompt": REMOTE_PROMPT_FIXTURE,
            "psyche": REMOTE_PSYCHE_FIXTURE,
        }[resolved.kind]
        fetched_file = tmp_path / "fetched" / resolved.repo.replace("/", "__") / fixture.name
        fetched_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture, fetched_file)
        return fetched_file, [fixture.name]

    monkeypatch.setattr("toolang.caps.github.resolve_github_cap_ref", fake_resolve)
    monkeypatch.setattr("toolang.caps.github.fetch_github_artifact", fake_fetch)

    prepared = prepare_agent(resolve_agent_ref("team/alice", cwd=tmp_path, toolang_root=root))
    caps = load_prepared_caps(prepared)

    assert prepared.program.get_decl("service", "github") is not None
    assert prepared.program.get_decl("prompt", "rewrite") is not None
    assert prepared.program.get_decl("prompt", "summarize") is not None
    assert prepared.program.get_decl("psyche", "reviewer") is not None
    github_service = prepared.program.get_decl("service", "github")
    assert github_service is not None
    assert github_service.body.startswith("---\n")
    assert [item.name for item in caps.services] == ["github"]
    assert isinstance(caps.services[0].front_matter, ServiceFrontmatter)
    assert caps.services[0].front_matter.target == "https://mcp.github.com/mcp"
    assert sorted(item.name for item in caps.prompts) == ["rewrite", "summarize"]
    assert [item.name for item in caps.psyches] == ["reviewer"]


def test_prepare_agent_can_disable_shared_and_global_caps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        """
use service by3hak/github

prompt summarize: ```md
Agent summary prompt.

{{input}}
```

thunk review(user):
    Review the issue.
""".strip(),
        encoding="utf-8",
    )
    shared_source_path(home).write_text("use prompt by3gus/rewrite\n", encoding="utf-8")
    global_source_path(root).write_text("use psyche by3hak/reviewer\n", encoding="utf-8")
    (shared_caps_dir(home, "service") / "github.md").parent.mkdir(parents=True)
    (shared_caps_dir(home, "service") / "github.md").write_text(
        REMOTE_SERVICE_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    def fake_resolve(kind: str, ref: str) -> CapRef:
        typed_kind = cast(CapKind, kind)
        owner, _, name = ref.partition("/")
        repo_name = {
            "service": "agent-services" if owner == "by3gus" else "services",
            "prompt": "agent-prompts" if owner == "by3gus" else "prompts",
            "psyche": "agent-psyches" if owner == "by3gus" else "psyches",
        }[typed_kind]
        path = {
            "service": f"services/{name}.md" if repo_name == "agent-services" else f"{name}.md",
            "prompt": f"prompts/{name}.md" if repo_name == "agent-prompts" else f"{name}.md",
            "psyche": f"psyches/{name}.md" if repo_name == "agent-psyches" else f"{name}.md",
        }[typed_kind]
        return CapRef(
            kind=typed_kind,
            name=name,
            ref=ref,
            repo=f"{owner}/{repo_name}",
            path=path,
            rev=f"rev-{owner}",
        )

    def fake_fetch(resolved: CapRef):
        import shutil

        fixture = {
            "service": REMOTE_SERVICE_FIXTURE,
            "prompt": REMOTE_PROMPT_FIXTURE,
            "psyche": REMOTE_PSYCHE_FIXTURE,
        }[resolved.kind]
        fetched_file = tmp_path / "fetched" / resolved.repo.replace("/", "__") / fixture.name
        fetched_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture, fetched_file)
        return fetched_file, [fixture.name]

    monkeypatch.setattr("toolang.caps.github.resolve_github_cap_ref", fake_resolve)
    monkeypatch.setattr("toolang.caps.github.fetch_github_artifact", fake_fetch)

    prepared = prepare_agent(
        resolve_agent_ref("team/alice", cwd=tmp_path, toolang_root=root),
        cap_scopes=CapScopeSelection(include_shared=False, include_global=False),
    )
    caps = load_prepared_caps(prepared)

    assert prepared.program.get_decl("service", "github") is not None
    assert prepared.program.get_decl("prompt", "summarize") is not None
    assert prepared.program.get_decl("prompt", "rewrite") is None
    assert prepared.program.get_decl("psyche", "reviewer") is None
    assert [item.name for item in caps.services] == ["github"]
    assert [item.name for item in caps.prompts] == ["summarize"]
    assert caps.psyches == []
