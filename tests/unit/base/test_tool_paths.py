"""Resolved paths retain logical anchors without expanding filesystem access."""

import asyncio
from dataclasses import replace

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.tool import ToolContext
from toolang.base.utils.paths import resolve_tool_path
from toolang.plugin.toolsets.loading import load_tools


def _context(home):
    return ToolContext(home, home, workspaces={"repo": home / "repo"})


@pytest.mark.parametrize("placement", ["host", "container"])
def test_explicit_anchor_is_independent_of_physical_layout(tmp_path, placement):
    home = tmp_path / placement
    context = _context(home)
    resolved = resolve_tool_path("/.hidden/../src/file", context, workspace="repo")
    assert resolved[1] == "repo"
    assert resolved[2] == "/src/file"
    assert resolved[0] == home / "repo/src/file"
    assert resolve_tool_path("/", context, workspace="repo")[2] == "/"
    assert resolve_tool_path("/.hidden", context, workspace="repo")[2] == "/.hidden"


def test_overlapping_workspaces_require_an_explicit_anchor(tmp_path):
    context = replace(
        _context(tmp_path),
        workspaces={"repo": tmp_path / "repo", "sdk": tmp_path / "repo/sdk"},
    )
    with pytest.raises(ToolangError, match="multiple workspaces"):
        resolve_tool_path("repo/sdk/file", context)
    repo = resolve_tool_path("/sdk/file", context, workspace="repo")
    sdk = resolve_tool_path("/file", context, workspace="sdk")
    assert repo[0] == sdk[0]
    assert (repo[1], repo[2]) == ("repo", "/sdk/file")
    assert (sdk[1], sdk[2]) == ("sdk", "/file")


def test_workspace_does_not_expand_home_permissions(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    context = replace(_context(home), workspaces={"outside": tmp_path})
    with pytest.raises(ToolangError, match="escapes agent home"):
        resolve_tool_path("secret", context, workspace="outside")
    (home / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ToolangError, match="escapes agent home"):
        resolve_tool_path("link/secret", context)
    with pytest.raises(ToolangError, match="escapes workspace"):
        resolve_tool_path("../secret", _context(home), workspace="repo")


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("fs__write", {"path": "workspace://repo/link/result", "text": "done"}),
        ("shell__execute", {"cwd": "repo/link", "command": "printf done > result"}),
    ],
)
def test_invocation_uses_the_resolved_path_not_a_retargeted_alias(
    tmp_path, tool_name, arguments
):
    first, second = tmp_path / "repo/first", tmp_path / "repo/second"
    first.mkdir(parents=True)
    second.mkdir()
    link = tmp_path / "repo/link"
    link.symlink_to(first, target_is_directory=True)
    tool = load_tools()[tool_name]
    context = _context(tmp_path)
    assert tool.touchpoints(arguments, context)
    link.unlink()
    link.symlink_to(second, target_is_directory=True)

    async def invoke():
        return (await tool.invoke(arguments, context)).output

    assert asyncio.run(invoke())
    assert (first / "result").read_text() == "done"
    assert not (second / "result").exists()


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("fs__list", {}),
        ("fs__read", {"path": "file"}),
        ("fs__write", {"path": "file", "text": "x"}),
        ("fs__append", {"path": "file", "text": "x"}),
        ("fs__glob", {}),
        ("fs__stat", {"path": "file"}),
        ("fs__mkdir", {"path": "dir"}),
        ("fs__remove", {"path": "file"}),
        ("shell__execute", {"command": "true"}),
    ],
)
def test_filesystem_tools_declare_their_defaulted_access_path(
    tmp_path, name, arguments
):
    context = _context(tmp_path)
    (tmp_path / "repo").mkdir()
    if name.startswith("fs__"):
        arguments = dict(arguments, workspace="repo")
    else:
        arguments = dict(arguments, cwd="repo")
    points = load_tools()[name].touchpoints(arguments, context)
    assert points is not None
    assert list(points) == ["repo"]
    assert points["repo"][0].startswith("/")


def test_shell_default_cwd_is_authorized(tmp_path):
    context = _context(tmp_path)
    tool = load_tools()["shell__execute"]
    assert tool.touchpoints({"command": "true"}, context) == {}
    assert asyncio.run(tool.invoke({"command": "pwd"}, context)).output["cwd"] == str(
        tmp_path
    )


def test_bare_aliases_use_physical_membership_and_existing_parent_semantics(tmp_path):
    (tmp_path / "repo/src").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(tmp_path / "repo/src", target_is_directory=True)
    context = _context(tmp_path)
    path = resolve_tool_path("alias/../file", context)
    assert path[0] == tmp_path / "repo/file"
    assert (path[1], path[2]) == ("repo", "/file")


def test_explicit_anchor_keeps_a_logical_symlink_path(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "repo/link").symlink_to(tmp_path / "shared", target_is_directory=True)
    path = resolve_tool_path("/link/file", _context(tmp_path), workspace="repo")
    assert path[0] == tmp_path / "shared/file"
    assert (path[1], path[2]) == ("repo", "/link/file")


def test_parent_segments_follow_symlinks_with_or_without_a_workspace(tmp_path):
    (tmp_path / "repo/actual/nested").mkdir(parents=True)
    (tmp_path / "repo/link").symlink_to(
        tmp_path / "repo/actual/nested", target_is_directory=True
    )
    context = _context(tmp_path)
    bare = resolve_tool_path("repo/link/../file", context)
    anchored = resolve_tool_path("/link/../file", context, workspace="repo")
    assert bare[0] == anchored[0] == tmp_path / "repo/actual/file"
    assert anchored[2] == "/actual/file"


def test_parent_segments_cannot_silently_select_a_different_anchor_target(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "shared/sub").mkdir(parents=True)
    (tmp_path / "repo/link").symlink_to(
        tmp_path / "shared/sub", target_is_directory=True
    )
    with pytest.raises(ToolangError, match="parent traversal through a symlink"):
        resolve_tool_path("/link/../file", _context(tmp_path), workspace="repo")


def test_unambiguous_parent_segments_preserve_the_logical_anchor(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "shared/sub").mkdir(parents=True)
    (tmp_path / "repo/link").symlink_to(tmp_path / "shared", target_is_directory=True)
    path = resolve_tool_path("/link/sub/../file", _context(tmp_path), workspace="repo")
    assert path[0] == tmp_path / "shared/file"
    assert (path[1], path[2]) == ("repo", "/link/file")
