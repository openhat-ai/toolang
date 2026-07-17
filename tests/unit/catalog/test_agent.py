"""Unit tests for authored resident-agent catalog behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from toolang.catalog import agent as agents
from toolang.common.github import GitHubRef


def test_agent_catalog_crud_and_clone_only_authored_files(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    catalog = agents.AgentCatalog(root)
    source = catalog.create("alice", source_text="agent source\n")
    (source.home / "skills" / "reviewer").mkdir(parents=True)
    (source.home / "skills" / "reviewer" / "SKILL.md").write_text(
        "# Reviewer\n", encoding="utf-8"
    )
    (source.home / ".caps").mkdir()
    (source.home / ".runtime").mkdir()

    cloned = catalog.clone("alice", "bob")

    assert cloned.program.read_text(encoding="utf-8") == "agent bob\n"
    assert (cloned.home / "skills" / "reviewer" / "SKILL.md").is_file()
    assert not (cloned.home / ".caps").exists()
    assert not (cloned.home / ".runtime").exists()
    assert [layout.name for layout in catalog.list()] == ["alice", "bob"]
    assert catalog.get("bob") == cloned
    assert catalog.remove("bob") == cloned
    assert catalog.get("bob") is None


def test_agent_selector_parsing_supports_name_shorthand_and_ref() -> None:
    local = agents.parse_agent_selector("alice")
    github_short = agents.parse_agent_selector("brice/alice")
    host_short = agents.parse_agent_selector("toolang.ai/alice")
    github_ref = agents.parse_agent_selector(
        "github://brice/agents/team/alice.too@main"
    )

    assert local.form == "name"
    assert local.name == "alice"
    assert github_short.form == "shorthand"
    assert github_short.github_owner == "brice"
    assert github_short.name == "alice"
    assert host_short.form == "shorthand"
    assert host_short.resolved_ref().render() == "https://toolang.ai/alice.too"
    assert github_ref.form == "ref"
    assert (
        github_ref.resolved_ref().render()
        == "github://brice/agents/team/alice.too@main"
    )


def test_agent_selector_parsing_supports_repo_shorthand() -> None:
    selector = agents.parse_agent_selector("brice/project/alice")

    assert selector.form == "shorthand"
    assert selector.github_owner == "brice"
    assert selector.github_repo == "project"
    assert selector.name == "alice"


def test_agent_selector_canonicalizes_raw_refs_heads_url() -> None:
    selector = agents.parse_agent_selector(
        "https://raw.githubusercontent.com/briceyan/agents/refs/heads/main/dev.too"
    )

    assert selector.form == "ref"
    assert (
        selector.resolved_ref().render()
        == "github://briceyan/agents/dev.too@refs/heads/main"
    )


def test_agent_shorthand_falls_back_to_main_when_branch_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[str] = []

    def fail_default_branch(owner: str, repo: str) -> str:
        del owner, repo
        raise ValueError("rate limited")

    def fake_exists(ref: GitHubRef) -> bool:
        probes.append(ref.render())
        return ref.path == "dev.too"

    monkeypatch.setattr(agents, "_github_repo_default_branch", fail_default_branch)
    monkeypatch.setattr(agents, "_github_agent_ref_exists", fake_exists)

    selector = agents.parse_agent_selector("briceyan/dev")
    ref = agents.resolve_agent_selector_ref(selector)

    assert ref.render() == "github://briceyan/agents/dev.too@main"
    assert probes == [
        "github://briceyan/agents/agents/dev.too@main",
        "github://briceyan/agents/dev.too@main",
    ]


def test_agent_shorthand_error_uses_input_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agents, "_github_repo_default_branch", lambda _owner, _repo: "main"
    )
    monkeypatch.setattr(agents, "_github_agent_ref_exists", lambda _ref: False)
    selector = agents.parse_agent_selector("briceyan/dev")

    with pytest.raises(
        ValueError, match="could not resolve agent shorthand: briceyan/dev"
    ):
        agents.resolve_agent_selector_ref(selector)


def test_github_agent_fetch_uses_raw_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_fetch(url: str) -> str:
        captured["url"] = url
        return "agent dev\n"

    monkeypatch.setattr(agents, "_fetch_http_text", fake_fetch)

    text = agents.fetch_agent_ref(
        GitHubRef(owner="briceyan", repo="agents", path="dev.too", rev="main")
    )

    assert text == "agent dev\n"
    assert (
        captured["url"]
        == "https://raw.githubusercontent.com/briceyan/agents/main/dev.too"
    )
