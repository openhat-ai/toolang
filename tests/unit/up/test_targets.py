"""Unit tests for agent selector and remote-source handling."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from toolang.common.layout import AgentLayout
from toolang.up import process as agents
from toolang.common.github import GitHubRef


def test_agent_selector_parsing_supports_name_shorthand_and_ref() -> None:
    local = agents.parse_agent_selector("alice")
    github_short = agents.parse_agent_selector("brice/alice")
    host_short = agents.parse_agent_selector("toolang.ai/alice")
    github_ref = agents.parse_agent_selector(
        "github://brice/agents/team/alice.too@main"
    )

    assert local.form == "name"
    assert local.name == "alice"
    assert github_short.github_owner == "brice"
    assert github_short.name == "alice"
    assert host_short.resolved_ref().render() == "https://toolang.ai/alice.too"
    assert github_ref.resolved_ref().render() == (
        "github://brice/agents/team/alice.too@main"
    )


def test_agent_selector_parsing_supports_repo_shorthand() -> None:
    selector = agents.parse_agent_selector("brice/project/alice")

    assert selector.github_owner == "brice"
    assert selector.github_repo == "project"
    assert selector.name == "alice"


def test_agent_selector_canonicalizes_raw_refs_heads_url() -> None:
    selector = agents.parse_agent_selector(
        "https://raw.githubusercontent.com/briceyan/agents/refs/heads/main/dev.too"
    )

    assert selector.resolved_ref().render() == (
        "github://briceyan/agents/dev.too@refs/heads/main"
    )


def test_agent_shorthand_falls_back_to_main_when_branch_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[str] = []
    monkeypatch.setattr(
        agents,
        "_github_repo_default_branch",
        lambda _owner, _repo: (_ for _ in ()).throw(ValueError("rate limited")),
    )

    def fake_exists(ref: GitHubRef) -> bool:
        probes.append(ref.render())
        return ref.path == "dev.too"

    monkeypatch.setattr(agents, "_github_agent_ref_exists", fake_exists)

    ref = agents.resolve_agent_selector_ref(agents.parse_agent_selector("briceyan/dev"))

    assert ref.render() == "github://briceyan/agents/dev.too@main"
    assert probes == [
        "github://briceyan/agents/agents/dev.too@main",
        "github://briceyan/agents/dev.too@main",
    ]


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
    assert captured["url"] == (
        "https://raw.githubusercontent.com/briceyan/agents/main/dev.too"
    )


def test_resolve_resident_layout_does_not_materialize_files(tmp_path: Path) -> None:
    layout = agents.resolve_run_layout(tmp_path / "toolang", "alice")

    assert layout == AgentLayout.resident(tmp_path / "toolang", "alice")
    assert layout.placement == "resident"
    assert not layout.home.exists()


def test_visiting_layout_derivation_does_not_resolve_or_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "brice/researcher"
    monkeypatch.setattr(
        agents,
        "resolve_agent_selector_ref",
        lambda *_args, **_kwargs: pytest.fail("history layout must not resolve"),
    )
    monkeypatch.setattr(
        agents,
        "fetch_agent_ref",
        lambda *_args, **_kwargs: pytest.fail("history layout must not fetch"),
    )

    assert agents.visiting_layout(selector) == AgentLayout.visiting(
        selector,
        "researcher",
    )


def test_resolve_visiting_layout_materializes_and_reuses_program(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = f"https://agents.example{tmp_path}/researcher.too"
    fetches: list[str] = []

    def fake_fetch(ref: agents.AgentRef, *, progress=None) -> str:
        del progress
        fetches.append(ref.render())
        return "agent researcher\n"

    monkeypatch.setattr(agents, "fetch_agent_ref", fake_fetch)

    first = agents.resolve_visiting_layout(source)
    second = agents.resolve_run_layout(tmp_path / "other", source)

    assert first is not second
    assert first == second == AgentLayout.visiting(source, "researcher")
    assert first.program.read_text(encoding="utf-8") == "agent researcher\n"
    assert fetches == [source]


def test_materialize_roaming_program_links_source_and_config(tmp_path: Path) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agent demo\n", encoding="utf-8")
    config = tmp_path / "toolang.toml"
    config.write_text("[models]\n", encoding="utf-8")

    layout = agents.materialize_roaming_program(source)

    assert layout == AgentLayout.roaming(source)
    assert layout.program.is_symlink()
    assert layout.config.is_symlink()
    assert (layout.program.parent / os.readlink(layout.program)).resolve() == source
    assert (layout.config.parent / os.readlink(layout.config)).resolve() == config
