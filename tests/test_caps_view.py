from __future__ import annotations

from pathlib import Path

from toolang.agent_refs import resolve_agent_ref
from toolang.caps_view import load_prepared_caps
from toolang.prepared import prepare_agent
from toolang.layout import resolve_toolang_root
from toolang.layout import global_source_path, shared_source_path
from toolang_caps.models import ResolvedCapRef

SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"


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

    fixture = Path(__file__).parent / "fixtures" / "remote-skill" / "pdf-processing"

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
        import shutil

        shutil.copytree(fixture, fetched_root)
        files = sorted(
            str(path.relative_to(fetched_root))
            for path in fetched_root.rglob("*")
            if path.is_file()
        )
        return fetched_root, files

    monkeypatch.setattr("toolang.sync.resolve_github_skill_ref", fake_resolve)
    monkeypatch.setattr("toolang.sync.fetch_github_tree", fake_fetch)

    prepared = prepare_agent(resolve_agent_ref("team/alice", cwd=tmp_path, toolang_root=root))
    caps = load_prepared_caps(prepared)

    assert [item.name for item in caps.skills] == ["repo-search"]
    assert caps.skills[0].ref == "by3hak/repo-search"
