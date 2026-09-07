"""Prepared paths retain logical anchors without expanding filesystem access."""

import asyncio
from dataclasses import replace

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.tool import ToolContext
from toolang.base.utils.function_tools import prepare_tool
from toolang.base.utils.paths import resolve_tool_path
from toolang.plugin.toolsets.loading import load_tools


def _context(home):
    return ToolContext("run_test", home, home, home, workspaces={"repo": home / "repo"})


@pytest.mark.parametrize("placement", ["host", "container"])
def test_explicit_anchor_is_independent_of_physical_layout(tmp_path, placement):
    home = tmp_path / placement
    context = _context(home)
    resolved = resolve_tool_path("/.hidden/../src/file", context, workspace="repo")
    assert resolved.workspace == "repo"
    assert resolved.relative == "/src/file"
    assert resolved.resolved == home / "repo/src/file"
    assert resolve_tool_path("/", context, workspace="repo").relative == "/"
    assert (
        resolve_tool_path("/.hidden", context, workspace="repo").relative == "/.hidden"
    )


def test_overlapping_workspaces_require_an_explicit_anchor(tmp_path):
    context = replace(
        _context(tmp_path),
        workspaces={"repo": tmp_path / "repo", "sdk": tmp_path / "repo/sdk"},
    )
    with pytest.raises(ToolangError, match="multiple workspaces"):
        resolve_tool_path("repo/sdk/file", context)
    repo = resolve_tool_path("/sdk/file", context, workspace="repo")
    sdk = resolve_tool_path("/file", context, workspace="sdk")
    assert repo.resolved == sdk.resolved
    assert (repo.workspace, repo.relative) == ("repo", "/sdk/file")
    assert (sdk.workspace, sdk.relative) == ("sdk", "/file")


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
def test_invocation_uses_the_prepared_path_not_a_retargeted_alias(
    tmp_path, tool_name, arguments
):
    first, second = tmp_path / "repo/first", tmp_path / "repo/second"
    first.mkdir(parents=True)
    second.mkdir()
    link = tmp_path / "repo/link"
    link.symlink_to(first, target_is_directory=True)
    prepared = prepare_tool(load_tools()[tool_name], arguments, _context(tmp_path))
    link.unlink()
    link.symlink_to(second, target_is_directory=True)

    async def invoke():
        return await prepared.invoke()

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
    context = replace(_context(tmp_path), wd=tmp_path / "repo")
    context.wd.mkdir()
    if name.startswith("fs__"):
        arguments = dict(arguments, workspace="repo")
    prepared = prepare_tool(load_tools()[name], arguments, context)
    (path,) = prepared.paths
    assert path.workspace == "repo"
    assert path.resolved.is_relative_to(context.wd)
    assert path.resolved.exists() == (path.resolved == context.wd)


def test_shell_default_cwd_is_authorized(tmp_path):
    context = replace(_context(tmp_path), wd=tmp_path.parent)
    with pytest.raises(ToolangError, match="escapes agent home"):
        prepare_tool(load_tools()["shell__execute"], {"command": "true"}, context)


def test_bare_aliases_use_physical_membership_and_existing_parent_semantics(tmp_path):
    (tmp_path / "repo/src").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(tmp_path / "repo/src", target_is_directory=True)
    context = _context(tmp_path)
    path = resolve_tool_path("alias/../file", context)
    assert path.resolved == tmp_path / "repo/file"
    assert (path.workspace, path.relative) == ("repo", "/file")


def test_explicit_anchor_keeps_a_logical_symlink_path(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "repo/link").symlink_to(tmp_path / "shared", target_is_directory=True)
    path = resolve_tool_path("/link/file", _context(tmp_path), workspace="repo")
    assert path.resolved == tmp_path / "shared/file"
    assert (path.workspace, path.relative) == ("repo", "/link/file")


def test_parent_segments_follow_symlinks_with_or_without_a_workspace(tmp_path):
    (tmp_path / "repo/actual/nested").mkdir(parents=True)
    (tmp_path / "repo/link").symlink_to(
        tmp_path / "repo/actual/nested", target_is_directory=True
    )
    context = _context(tmp_path)
    bare = resolve_tool_path("repo/link/../file", context)
    anchored = resolve_tool_path("/link/../file", context, workspace="repo")
    assert bare.resolved == anchored.resolved == tmp_path / "repo/actual/file"
    assert anchored.relative == "/actual/file"


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
    assert path.resolved == tmp_path / "shared/file"
    assert (path.workspace, path.relative) == ("repo", "/link/file")
