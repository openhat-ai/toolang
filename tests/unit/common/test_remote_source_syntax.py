"""Explicit source syntax and Agent Name boundaries."""

import pytest

from toolang.common.github import GitHubRef, GitHubShorthand
from toolang.common.layout import derive_agent_name, validate_agent_name
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
    assert source.default_name() == "Daily Report.v2"


@pytest.mark.parametrize("name", ["a", "a" * 64, "daily_report", "a0", "a__"])
def test_explicit_agent_names_are_preserved(name):
    assert validate_agent_name(name) == name
    assert derive_agent_name(name) == name


@pytest.mark.parametrize(
    "name", ["a" * 65, "Gus", "gus-by3", "a.b", "_a", "1a", "a ", "中文", ""]
)
def test_explicit_agent_names_are_not_normalized(name):
    with pytest.raises(ValueError, match="invalid agent name"):
        validate_agent_name(name)


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("Daily-Report.v2", "daily_report_v2"),
        ("2026 report", "script_2026_report"),
        ("Some 中文 Name", "some_name"),
        ("__Gus__", "gus"),
    ],
)
def test_derived_names_normalize_characters(stem, expected):
    assert derive_agent_name(stem) == expected


@pytest.mark.parametrize("stem", ["中文", "---", "A" * 65, "1" * 58])
def test_derived_names_reject_empty_or_overlong_results(stem):
    with pytest.raises(ValueError, match="invalid agent name"):
        derive_agent_name(stem)


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
