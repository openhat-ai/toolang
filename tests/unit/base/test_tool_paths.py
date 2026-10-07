"""Path-aware tools retain the selected Run root from preflight through invocation."""

import asyncio
from dataclasses import replace

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.tool import ToolContext
from toolang.base.utils.workspace_paths import resolve_input_path
from toolang.plugin.toolsets.loading import load_tools


def _context(tmp_path, *, cwd="repo://"):
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    return ToolContext(tmp_path, tmp_path, workspaces={"repo": root}, cwd=cwd)


def test_rooted_paths_are_independent_of_run_location(tmp_path):
    context = _context(tmp_path, cwd="")
    root = tmp_path / "repo"
    assert resolve_input_path("repo://src/main.py", context) == (
        root / "src/main.py",
        "repo",
        "/src/main.py",
    )
    assert resolve_input_path("repo://", context) == (root, "repo", "/")
    with pytest.raises(
        ToolangError, match="relative path requires a current workspace"
    ):
        resolve_input_path("src/main.py", context)


def test_nested_absolute_chooses_deepest_root_and_named_path_preserves_identity(
    tmp_path,
):
    root = tmp_path / "repo"
    sdk = root / "sdk"
    sdk.mkdir(parents=True)
    context = replace(_context(tmp_path), workspaces={"repo": root, "sdk": sdk})
    assert resolve_input_path(str(sdk / "file"), context) == (
        sdk / "file",
        "sdk",
        "/file",
    )
    assert resolve_input_path("repo://sdk/file", context) == (
        sdk / "file",
        "repo",
        "/sdk/file",
    )
    assert resolve_input_path("sdk/file", context) == (
        sdk / "file",
        "repo",
        "/sdk/file",
    )


def test_named_paths_reject_symlink_escape_and_parent_traversal(tmp_path):
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "repo/escape").symlink_to(outside, target_is_directory=True)
    for value in ("repo://escape/file", "../outside/file"):
        with pytest.raises(ToolangError, match="escapes workspace"):
            resolve_input_path(value, context)


@pytest.mark.parametrize(
    "name",
    [
        "fs__list",
        "fs__glob",
        "fs__read",
        "fs__write",
        "fs__append",
        "fs__stat",
        "fs__mkdir",
        "fs__remove",
        "shell__execute",
    ],
)
def test_path_aware_tools_preflight_the_run_location(tmp_path, name):
    context = _context(tmp_path)
    args = {"command": "true"} if name == "shell__execute" else {"path": "file"}
    if name in {"fs__write", "fs__append"}:
        args["text"] = "x"
    if name in {"fs__list", "fs__glob"}:
        args = {}
    tool = load_tools()[name]
    assert tool.paths(args, context) == {
        "repo": (
            "/" if name in {"fs__list", "fs__glob", "shell__execute"} else "/file",
        )
    }


@pytest.mark.parametrize("name", ["fs__write", "shell__execute"])
def test_preflight_target_cannot_be_retargeted_by_a_symlink(tmp_path, name):
    context = _context(
        tmp_path, cwd="repo://link" if name == "shell__execute" else "repo://"
    )
    root = tmp_path / "repo"
    first, second = root / "first", root / "second"
    first.mkdir()
    second.mkdir()
    link = root / "link"
    link.symlink_to(first, target_is_directory=True)
    tool = load_tools()[name]
    args = (
        {"command": "printf done > result"}
        if name == "shell__execute"
        else {"path": "repo://link/result", "text": "done"}
    )
    assert tool.paths(args, context)
    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    assert asyncio.run(tool.invoke(args, context)).output
    assert (first / "result").read_text() == "done"
    assert not (second / "result").exists()


def test_shell_requires_a_selected_cwd(tmp_path):
    tool = load_tools()["shell__execute"]
    with pytest.raises(ToolangError, match="requires a current workspace"):
        tool.paths({"command": "pwd"}, _context(tmp_path, cwd=""))
    assert tool.paths({"command": "pwd"}, _context(tmp_path)) == {"repo": ("/",)}
    output = asyncio.run(tool.invoke({"command": "pwd"}, _context(tmp_path))).output
    assert output["cwd"] == "repo://"
    assert output["stdout"].strip() == str(tmp_path / "repo")


def test_only_workspace_uris_and_dot_relative_paths_are_path_syntax(tmp_path):
    from toolang.base.utils.workspace_paths import resolve_input_path

    repo = tmp_path / "repo"
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    file_root = tmp_path / "file-workspace"
    file_root.mkdir()
    context = ToolContext(
        tmp_path,
        tmp_path,
        workspaces={"repo": repo, "file": file_root},
        cwd="repo://src/deep",
    )
    assert resolve_input_path("repo://", context) == (repo, "repo", "/")
    assert resolve_input_path(".", context) == (nested, "repo", "/src/deep")
    assert resolve_input_path("../", context) == (repo / "src", "repo", "/src")
    assert resolve_input_path("../../", context) == (repo, "repo", "/")
    assert resolve_input_path("./child", context) == (
        nested / "child",
        "repo",
        "/src/deep/child",
    )
    # A file://-looking prefix only means a workspace literally named file.
    assert resolve_input_path("file://child", context) == (
        file_root / "child",
        "file",
        "/child",
    )
    with pytest.raises(ToolangError, match="workspace path must be relative"):
        resolve_input_path("file:///tmp", context)
    with pytest.raises(ToolangError, match="outside configured workspaces"):
        resolve_input_path("/tmp/not-a-grant", context)
