"""The cwd alias resolves to a captured grant, never a process directory."""

import asyncio
from dataclasses import replace

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.tool import ToolContext
from toolang.base.utils.workspace_paths import capture_cwd, parse_workspace_uri
from toolang.plugin.toolsets.loading import load_tools


@pytest.fixture
def fs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sub").mkdir()
    context = ToolContext(
        "run_test", tmp_path, tmp_path, tmp_path, workspaces={"repo": repo}
    )
    return load_tools(queries=("fs/*",)), context, repo


def invoke(fs, context, name, **arguments):
    return asyncio.run(fs[0][f"fs__{name}"].invoke(arguments, context))


@pytest.mark.parametrize("suffix", ["", "/sub"])
def test_cwd_alias_uses_real_workspace_identity_for_every_operation(fs, suffix):
    _tools, context, repo = fs
    cwd = capture_cwd(repo / suffix.lstrip("/"), {"repo": str(repo)})
    context = replace(context, cwd=cwd)
    prefix = "workspace://repo" + suffix
    location = prefix if suffix else prefix + "/"
    assert parse_workspace_uri("workspace://.") == (".", "/")
    assert invoke(fs, context, "list", path="workspace://.")["path"] == location
    assert invoke(fs, context, "list", path="workspace://")["cwd"] == "workspace://./"
    invoke(fs, context, "mkdir", path="workspace://./dir")
    uri = "workspace://./dir/a%20b"
    assert (
        invoke(fs, context, "write", path=uri, text="a")["path"]
        == prefix + "/dir/a%20b"
    )
    invoke(fs, context, "append", path=uri, text="b")
    assert invoke(fs, context, "read", path=uri)["text"] == "ab"
    assert invoke(fs, context, "stat", path=uri)["is_file"]
    assert invoke(fs, context, "glob", path="workspace://./dir", pattern="*")[
        "matches"
    ] == [prefix + "/dir/a%20b"]
    invoke(fs, context, "remove", path=uri)
    assert not (cwd.resolved / "dir/a b").exists()


def test_temporary_cwd_requires_a_grant_and_cannot_escape(fs):
    _tools, context, repo = fs
    cwd = capture_cwd(repo, {})
    assert cwd.workspace == "."
    context = replace(context, cwd=cwd, workspaces={".": repo})
    assert (
        invoke(fs, context, "write", path="workspace://./file", text="ok")["path"]
        == "workspace://./file"
    )
    for path in ("workspace://./../secret", "workspace://./%2e%2e/secret"):
        with pytest.raises(ToolangError, match="escapes workspace"):
            invoke(fs, context, "write", path=path, text="bad")
    (repo / "escape").symlink_to(repo.parent, target_is_directory=True)
    with pytest.raises(ToolangError, match="escapes workspace"):
        invoke(fs, context, "write", path="workspace://./escape/secret", text="bad")
    with pytest.raises(ToolangError, match="cannot remove a workspace root"):
        invoke(fs, context, "remove", path="workspace://.", recursive=True)
    with pytest.raises(ToolangError, match="not available"):
        invoke(fs, replace(context, workspaces={}), "read", path="workspace://./file")


def test_missing_or_remapped_cwd_never_falls_back_to_a_temporary_root(fs, tmp_path):
    _tools, context, repo = fs
    with pytest.raises(ToolangError, match="no cwd"):
        invoke(fs, context, "list", path="workspace://.")
    context = replace(context, cwd=capture_cwd(repo, {"repo": str(repo)}))
    with pytest.raises(ToolangError, match="not available"):
        invoke(fs, replace(context, workspaces={}), "list", path="workspace://.")
    with pytest.raises(ToolangError, match="recorded location"):
        invoke(
            fs,
            replace(context, workspaces={"repo": tmp_path}),
            "list",
            path="workspace://.",
        )


def test_alias_traversal_uses_the_real_workspace_boundary(fs):
    _tools, context, repo = fs
    context = replace(context, cwd=capture_cwd(repo / "sub", {"repo": str(repo)}))
    assert (
        invoke(fs, context, "write", path="workspace://./../file", text="ok")["path"]
        == "workspace://repo/file"
    )
    with pytest.raises(ToolangError, match="escapes workspace"):
        invoke(fs, context, "write", path="workspace://./../../file", text="bad")


def test_namespace_cwd_does_not_redirect_after_workspace_remapping(fs, tmp_path):
    _tools, context, repo = fs
    context = replace(context, cwd=capture_cwd(repo, {"repo": str(repo)}))
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    context = replace(context, workspaces={"repo": replacement})
    location = invoke(fs, context, "list", path="workspace://")["cwd"]
    with pytest.raises(ToolangError, match="recorded location"):
        invoke(fs, context, "write", path=location + "/wrong", text="bad")
    assert not (replacement / "wrong").exists()
