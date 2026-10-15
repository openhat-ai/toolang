"""Explicit remote Source Ref syntax."""

import pytest

from toolang.common.github import GitHubRef, GitHubShorthand
from toolang.common.source import HttpRef, parse_remote_source


@pytest.mark.parametrize("prefix", ["gh:", "github:", "github://"])
def test_exact_github_aliases_have_one_canonical_ref(prefix):
    assert parse_remote_source(
        f"{prefix}owner/repo/path/agent.too@refs/heads/main"
    ) == GitHubRef("owner", "repo", "path/agent.too", "refs/heads/main")


@pytest.mark.parametrize("prefix", ["gh:", "github:"])
def test_github_shorthand_keeps_repository_probing_with_the_caller(prefix):
    assert parse_remote_source(f"{prefix}owner/some-name") == GitHubShorthand(
        "owner", "some-name"
    )
    assert parse_remote_source(f"{prefix}owner/repo/some-name") == GitHubShorthand(
        "owner", "some-name", "repo"
    )


def test_complete_http_url_preserves_query_and_filename():
    url = "https://example.com/Daily%20Report.v2.too?version=3"
    source = parse_remote_source(url)
    assert isinstance(source, HttpRef)
    assert source == HttpRef(url)
    assert source.path == "/Daily Report.v2.too"
    assert source.default_name() == "Daily Report.v2"


@pytest.mark.parametrize(
    "ref",
    [
        "gh:",
        "github:",
        "gh:owner",
        "gh:owner//name",
        "gh:owner/repo/path/more",
        "gh:owner/repo@main",
        "gh:other:owner/name",
    ],
)
def test_malformed_github_aliases_are_rejected(ref):
    with pytest.raises(ValueError, match="GitHub"):
        parse_remote_source(ref)
