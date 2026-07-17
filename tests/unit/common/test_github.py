from __future__ import annotations

import pytest

from toolang.common.github import (
    GitHubRef,
    github_raw_url,
    parse_github_file_url,
    parse_github_ref,
    parse_github_url,
)


def test_github_ref_renders_canonical_uri_and_default_name() -> None:
    ref = GitHubRef(
        owner="acme",
        repo="agents",
        path="agents/reviewer.too",
        rev="main",
    )

    assert ref.render() == "github://acme/agents/agents/reviewer.too@main"
    assert ref.default_name() == "reviewer"


def test_parse_github_ref_requires_explicit_revision() -> None:
    ref = parse_github_ref("github://acme/agents/skills/reviewer@refs/heads/main")

    assert ref == GitHubRef(
        owner="acme",
        repo="agents",
        path="skills/reviewer",
        rev="refs/heads/main",
    )
    with pytest.raises(ValueError, match="must include @rev"):
        parse_github_ref("github://acme/agents/skills/reviewer")


@pytest.mark.parametrize("view", ["blob", "tree"])
def test_parse_github_url_supports_web_views(view: str) -> None:
    ref = parse_github_url(
        f"https://github.com/acme/agents/{view}/main/skills/reviewer"
    )

    assert ref == GitHubRef(
        owner="acme",
        repo="agents",
        path="skills/reviewer",
        rev="main",
    )


def test_parse_github_url_supports_raw_refs_heads_url() -> None:
    ref = parse_github_url(
        "https://raw.githubusercontent.com/acme/agents/refs/heads/main/dev.too"
    )

    assert ref == GitHubRef(
        owner="acme",
        repo="agents",
        path="dev.too",
        rev="refs/heads/main",
    )


def test_parse_github_file_url_rejects_tree_view() -> None:
    with pytest.raises(ValueError, match="invalid GitHub URL"):
        parse_github_file_url(
            "https://github.com/acme/agents/tree/main/agents/reviewer.too"
        )


def test_parse_github_url_ignores_other_hosts() -> None:
    assert parse_github_url("https://example.com/acme/agents/main/dev.too") is None


def test_github_raw_url_quotes_revision_and_path() -> None:
    ref = GitHubRef(
        owner="acme",
        repo="agents",
        path="agents/reviewer file.too",
        rev="refs/heads/feature branch",
    )

    assert github_raw_url(ref) == (
        "https://raw.githubusercontent.com/acme/agents/"
        "refs/heads/feature%20branch/agents/reviewer%20file.too"
    )
