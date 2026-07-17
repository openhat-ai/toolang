"""Unit tests for authored cap catalog behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from toolang.catalog import cap as caps


def test_cap_catalog_collapses_skill_directories(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    skill = root / "skills" / "reviewer"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Reviewer\n", encoding="utf-8")
    (skill / "notes.txt").write_text("asset\n", encoding="utf-8")
    (root / "prompts").mkdir()
    (root / "prompts" / "rewrite.md").write_text("# Rewrite\n", encoding="utf-8")

    entries = caps.CapCatalog(root, "alice", visibility="shared").list()

    assert [(entry.kind, entry.path) for entry in entries] == [
        ("prompt", "prompts/rewrite.md"),
        ("skill", "skills/reviewer/SKILL.md"),
    ]


def test_cap_catalog_crud_is_scoped_to_authored_files(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    shared = caps.CapCatalog(root, "alice", visibility="shared")
    private = caps.CapCatalog(root, "alice", visibility="private")

    prompt_path = shared.create(
        "prompt",
        "rewrite",
        "---\ndescription: Rewrite text\n---\nRewrite this text.\n",
    )
    skill_path = private.create(
        "skill",
        "reviewer",
        "---\ndescription: Review code\n---\nReview code carefully.\n",
    )
    service_path = shared.create(
        "service",
        "linear",
        "---\n"
        "description: Linear MCP\n"
        "transport: stdio\n"
        "target: uvx mcp-remote https://mcp.linear.app/sse\n"
        "env: LINEAR_API_KEY, API_KEY\n"
        "---\n",
    )

    assert prompt_path == root / "prompts" / "rewrite.md"
    assert skill_path == root / "agents" / "alice" / "skills" / "reviewer" / "SKILL.md"
    assert service_path == root / "services" / "linear.md"
    assert [(entry.kind, entry.meta["description"]) for entry in shared.list()] == [
        ("prompt", "Rewrite text"),
        ("service", "Linear MCP"),
    ]
    assert [(entry.kind, entry.path) for entry in private.list()] == [
        ("skill", "agents/alice/skills/reviewer/SKILL.md")
    ]

    assert shared.remove("prompt", "rewrite") is True
    assert shared.remove("service", "linear") is True
    assert private.remove("skill", "reviewer") is True
    assert shared.list() == ()
    assert private.list() == ()


def test_cap_catalog_rejects_service_env_map(tmp_path: Path) -> None:
    catalog = caps.CapCatalog(tmp_path / "toolang", "alice", visibility="shared")

    with pytest.raises(
        ValueError, match="service env must list environment variable names"
    ):
        catalog.create(
            "service",
            "linear",
            "---\n"
            "description: Linear MCP\n"
            "transport: stdio\n"
            "target: uvx mcp-remote https://mcp.linear.app/sse\n"
            "env:\n"
            "  LINEAR_API_KEY: $LINEAR_API_KEY\n"
            "---\n",
        )


def test_cap_catalog_lists_wired_entries_without_preparing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "resolve_remote_ref", lambda _kind, _ref, **_kwargs: "github://acme/caps/skills/reviewer@main")
    catalog = caps.CapCatalog(root, "alice", visibility="private")
    catalog.create(
        "prompt",
        "rewrite",
        "---\ndescription: Rewrite text\n---\nRewrite this text.\n",
    )
    caps.add_remote_entry(
        root,
        "alice",
        visibility="private",
        kind="skill",
        ref="acme/reviewer",
    )

    entries = catalog.list()

    assert [(entry.kind, entry.name, entry.form, entry.origin) for entry in entries] == [
        ("prompt", "rewrite", "file", "local"),
        ("skill", "reviewer", "wired", "remote"),
    ]
    assert not (root / "agents" / "alice" / ".caps").exists()


def test_remote_shorthand_falls_back_to_main_when_branch_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        caps,
        "_github_repo_default_branch",
        lambda _owner, _repo: (_ for _ in ()).throw(ValueError("rate limited")),
    )
    monkeypatch.setattr(
        caps,
        "_github_remote_exists",
        lambda kind, ref: (
            kind == "psyche"
            and ref == "github://briceyan/agents/psyches/senior-engineer.md@main"
        ),
    )

    assert (
        caps.resolve_remote_ref("psyche", "briceyan/senior-engineer")
        == "github://briceyan/agents/psyches/senior-engineer.md@main"
    )
