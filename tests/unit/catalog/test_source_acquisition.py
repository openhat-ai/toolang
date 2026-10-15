"""Upstream version and payload rules belong to Source acquisition."""

import json

import pytest

from toolang.catalog import source
from toolang.common.github import GitHubRef


def test_github_pins_before_fetch_and_reuses_unchanged_commit(monkeypatch):
    sha = "a" * 40
    commits, fetched = [], []
    monkeypatch.setattr(
        source, "_resolve_github_commit", lambda ref: commits.append(ref.rev) or sha
    )
    monkeypatch.setattr(
        source,
        "_fetch_github_directory",
        lambda ref: (
            fetched.append(ref.rev)
            or {"SKILL.md": b"Review.", "scripts/check.py": b"pass\n"}
        ),
    )
    ref = "gh:owner/repo/skills/review@main"
    with source.acquisition_session(refresh=True):
        captured = source.acquire("skill", ref)
        assert source.acquire("skill", ref, cached=captured) is captured
    assert commits == ["main"]
    assert fetched == [sha]
    assert captured.resolved_ref == f"github://owner/repo/skills/review@{sha}"
    assert captured.entry == "skills/review/SKILL.md"
    assert set(captured.files) == {
        "skills/review/SKILL.md",
        "skills/review/scripts/check.py",
    }


def test_http_same_url_requires_fetched_byte_comparison(monkeypatch):
    content = [b"First."]
    calls = []

    def fetch(url):
        calls.append(url)
        return content[0]

    monkeypatch.setattr(source, "_fetch_url_bytes", fetch)
    ref = "https://example.com/style.md?version=latest"
    captured = source.acquire("prompt", ref)
    assert source.acquire("prompt", ref, cached=captured) is captured
    content[0] = b"Second."
    changed = source.acquire("prompt", ref, cached=captured, refresh=True)
    assert changed.resolved_ref == captured.resolved_ref
    assert changed.files != captured.files
    assert calls == [ref, ref]


def test_github_commit_response_is_validated(monkeypatch, offline_source_versions):
    ref = GitHubRef("owner", "repo", "agent.too", "main")
    calls = []

    def fetch(url):
        calls.append(url)
        return json.dumps({"sha": "b" * 40}).encode()

    monkeypatch.setattr(source, "_fetch_url_bytes", fetch)
    assert offline_source_versions(ref) == "b" * 40
    assert calls == ["https://api.github.com/repos/owner/repo/commits/main"]
    monkeypatch.setattr(source, "_fetch_url_bytes", lambda url: b'{"sha":"main"}')
    with pytest.raises(ValueError, match="invalid GitHub commit"):
        offline_source_versions(ref)


def test_full_sha_does_not_require_commit_lookup(offline_source_versions):
    assert (
        offline_source_versions(GitHubRef("owner", "repo", "agent.too", "c" * 40))
        == "c" * 40
    )
