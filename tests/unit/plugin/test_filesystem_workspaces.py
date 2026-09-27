"""Workspace URI tools use captured grants and return reusable logical paths."""

import asyncio
from dataclasses import replace

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.tool import ToolContext
from toolang.base.utils.workspace_paths import (
    parse_cwd,
    resolve_input_path,
    workspace_uri,
)
from toolang.plugin.toolsets.loading import load_tools


@pytest.fixture
def fs(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    context = ToolContext(
        home, home, workspaces={"repo": repo, "unavailable": tmp_path / "missing"}
    )
    return load_tools(queries=("fs/*", "shell/*")), context, repo


def _invoke(fs, name, **arguments):
    tools, context, _repo = fs
    return asyncio.run(tools[f"fs__{name}"].invoke(arguments, context)).output


def test_all_filesystem_operations_and_uri_results(fs):
    _tools, _context, repo = fs
    with pytest.raises(ToolangError, match="workspace is not available: workspace"):
        _invoke(fs, "list", path="workspace://")
    assert _invoke(fs, "mkdir", path="repo://src")["path"] == "repo://src"
    uri = "repo://src/a%20%23%3F%25%E4%B8%AD.txt"
    assert _invoke(fs, "write", path=uri, text="first")["path"] == uri
    assert _invoke(fs, "append", path=uri, text=" second")["path"] == uri
    assert _invoke(fs, "read", path=uri)["text"] == "first second"
    assert _invoke(fs, "stat", path=uri)["is_file"]
    listed = _invoke(fs, "list", path="repo://src")
    assert listed["entries"] == [{"name": "a #?%中.txt", "path": uri, "is_dir": False}]
    assert _invoke(fs, "glob", path="repo://", pattern="**/*.txt")["matches"] == [uri]
    assert _invoke(fs, "glob", path="repo://", pattern="*.txt", recursive=True)[
        "matches"
    ] == [uri]
    assert (
        _invoke(fs, "read", path=listed["entries"][0]["path"])["text"] == "first second"
    )
    _invoke(fs, "remove", path=uri)
    assert not _invoke(fs, "stat", path=uri)["exists"]
    _invoke(fs, "remove", path="repo://src", recursive=True)
    assert list(repo.iterdir()) == []


@pytest.mark.parametrize(
    "value",
    [
        "workspace:/repo/file",
        "workspace:///file",
        "workspace://Repo/file",
        "workspace://user@repo/file",
        "repo://file?query",
        "repo://file#fragment",
        "repo://file%",
        "repo://file%GG",
        "repo://%FF",
        "repo://%00",
        "runspace://coop/file",
        "WORKSPACE://repo/file",
    ],
)
def test_invalid_uri_never_falls_back_to_a_home_path(fs, value):
    with pytest.raises(ToolangError):
        _invoke(fs, "write", path=value, text="bad")
    assert list(fs[1].home.iterdir()) == []


def test_uri_decodes_once_and_keeps_host_container_identity(fs):
    assert parse_cwd("repo://%252e%252e") == ("repo", "%2e%2e")
    assert workspace_uri("repo", "/ #?%中") == "repo://%20%23%3F%25%E4%B8%AD"
    _invoke(fs, "write", path="repo://%252e%252e", text="literal")
    assert (fs[2] / "%2e%2e").read_text() == "literal"
    assert _invoke(fs, "stat", path="repo://")["path"] == "repo://"


@pytest.mark.parametrize("path", ["repo://../secret", "repo://%2e%2e/secret"])
def test_parent_traversal_cannot_escape(fs, path):
    with pytest.raises(ToolangError, match="escapes workspace"):
        _invoke(fs, "write", path=path, text="bad")


@pytest.mark.parametrize(
    "name", ["read", "write", "append", "stat", "mkdir", "remove", "glob"]
)
def test_namespace_uri_is_just_a_workspace_reference(fs, name):
    with pytest.raises(ToolangError, match="workspace is not available: workspace"):
        _invoke(fs, name, path="workspace://", text="bad")


def test_grants_must_exist_and_root_removal_is_rejected(fs):
    for name in ("unknown", "unavailable"):
        with pytest.raises(ToolangError, match="not available"):
            _invoke(fs, "write", path=f"{name}://file", text="bad")
    assert not fs[1].workspaces["unavailable"].exists()
    with pytest.raises(ToolangError, match="cannot remove a workspace root"):
        _invoke(fs, "remove", path="repo://", recursive=True)
    with pytest.raises(ToolangError, match="path, not workspace"):
        _invoke(fs, "read", path="repo://file", workspace="repo")


def test_symlinks_and_glob_cannot_escape_workspace(fs):
    tools, context, repo = fs
    outside = context.home / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    (repo / "escape").symlink_to(outside, target_is_directory=True)
    for name, arguments in [
        ("read", {"path": "repo://escape/secret"}),
        ("write", {"path": "repo://escape/new", "text": "bad"}),
        ("list", {"path": "repo://"}),
        ("glob", {"path": "repo://", "pattern": "escape/*"}),
        ("glob", {"path": "repo://", "recursive": True}),
    ]:
        with pytest.raises(ToolangError, match="escapes workspace"):
            asyncio.run(tools[f"fs__{name}"].invoke(arguments, context)).output
    assert not (outside / "new").exists()


@pytest.mark.parametrize("pattern", ["../*", "/tmp/*", "a/../../*", ""])
def test_glob_patterns_stay_in_the_selected_directory(fs, pattern):
    with pytest.raises(ToolangError, match="glob pattern"):
        _invoke(fs, "glob", path="repo://", pattern=pattern)


def test_internal_aliases_and_parent_components_keep_path_meaning(fs):
    _tools, _context, repo = fs
    (repo / "actual/nested").mkdir(parents=True)
    (repo / "link").symlink_to(repo / "actual/nested", target_is_directory=True)
    _invoke(fs, "write", path="repo://link/../file", text="correct")
    assert (repo / "actual/file").read_text() == "correct"
    assert not (repo / "file").exists()
    _invoke(fs, "write", path="repo://link/note", text="alias")
    assert (
        _invoke(fs, "list", path="repo://link")["entries"][0]["path"]
        == "repo://link/note"
    )


def test_nested_workspaces_keep_the_explicit_uri_identity(fs):
    tools, context, repo = fs
    nested = repo / "sdk"
    nested.mkdir()
    context = replace(context, workspaces={"repo": repo, "sdk": nested})
    for uri, workspace, relative in [
        ("repo://sdk/file", "repo", "/sdk/file"),
        ("sdk://file", "sdk", "/file"),
    ]:
        arguments = {"path": uri, "text": "done"}
        tool = tools["fs__write"]
        assert tool.paths(arguments, context) == {workspace: (relative,)}
        assert asyncio.run(tool.invoke(arguments, context)).output["path"] == uri


def test_glob_normalizes_directory_patterns_without_following_aliases(fs):
    _tools, _context, repo = fs
    (repo / "src/nested").mkdir(parents=True)
    (repo / "src/file").write_text("x")
    (repo / "alias").symlink_to(repo / "src", target_is_directory=True)
    assert _invoke(fs, "glob", path="repo://", pattern="./src/*/")["matches"] == [
        "repo://src/nested"
    ]
    assert _invoke(fs, "glob", path="repo://", pattern="alias/*")["matches"] == []
    with pytest.raises(ToolangError, match="invalid glob pattern"):
        _invoke(fs, "glob", path="repo://", pattern="a**b")


def test_recursive_glob_ignores_unmatched_external_symlinks(fs):
    _tools, context, repo = fs
    (repo / "src").mkdir()
    (repo / "src/main.py").write_text("pass")
    outside = context.home / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("private")
    (repo / "dependencies").symlink_to(outside, target_is_directory=True)
    (repo / "interpreter").symlink_to(outside / "secret.py")

    assert _invoke(fs, "glob", path="repo://", pattern="**/*.py")["matches"] == [
        "repo://src/main.py"
    ]


@pytest.mark.parametrize("pattern", ["**/*.py", "**/**/**/*.py"])
def test_recursive_glob_collapses_redundant_recursive_components(
    fs, monkeypatch, pattern
):
    _tools, _context, repo = fs
    leaf = repo / "a/b/c/d/e/f"
    leaf.mkdir(parents=True)
    (leaf / "main.py").write_text("pass")
    visits = {}
    path_type = type(repo)
    original_iterdir = path_type.iterdir

    def iterdir(path):
        visits[path] = visits.get(path, 0) + 1
        return original_iterdir(path)

    monkeypatch.setattr(path_type, "iterdir", iterdir)
    assert _invoke(fs, "glob", path="repo://", pattern=pattern, recursive=True)[
        "matches"
    ] == ["repo://a/b/c/d/e/f/main.py"]
    assert max(visits.values()) <= 2


@pytest.mark.parametrize("kind", ["file", "directory", "missing"])
def test_remove_unlinks_the_addressed_symlink_not_its_target(fs, kind):
    _tools, _context, repo = fs
    target = repo / "target"
    if kind == "directory":
        target.mkdir()
        (target / "keep").write_text("keep")
    elif kind == "file":
        target.write_text("keep")
    link = repo / "alias"
    link.symlink_to(target, target_is_directory=kind == "directory")

    assert _invoke(fs, "remove", path="repo://alias", recursive=True) == {
        "path": "repo://alias",
        "removed": True,
    }
    assert not link.is_symlink()
    if kind != "missing":
        assert (
            target / "keep" if kind == "directory" else target
        ).read_text() == "keep"


def test_remove_can_unlink_an_alias_to_the_workspace_root(fs):
    _tools, _context, repo = fs
    link = repo / "self"
    link.symlink_to(repo, target_is_directory=True)
    _invoke(fs, "remove", path="repo://self", recursive=True)
    assert repo.is_dir()
    assert not link.is_symlink()


def test_remove_cannot_unlink_an_external_entry_pointing_into_the_workspace(fs):
    _tools, context, repo = fs
    target = repo / "target"
    target.write_text("keep")
    outside = context.home / "outside"
    outside.mkdir()
    link = outside / "back"
    link.symlink_to(target)
    (repo / "bridge").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolangError, match="escapes workspace"):
        _invoke(fs, "remove", path="repo://bridge/back")
    assert target.read_text() == "keep"
    assert link.is_symlink()


def test_remove_keeps_the_parent_directory_when_an_alias_changes(fs):
    tools, context, repo = fs
    for name in ("first", "second"):
        directory = repo / name
        directory.mkdir()
        (directory / "target").write_text("keep")
        (directory / "link").symlink_to(directory / "target")
    parent = repo / "parent"
    parent.symlink_to(repo / "first", target_is_directory=True)
    tool = tools["fs__remove"]
    arguments = {"path": "repo://parent/link"}
    assert tool.paths(arguments, context) == {"repo": ("/parent/link",)}
    parent.unlink()
    parent.symlink_to(repo / "second", target_is_directory=True)
    assert (
        asyncio.run(tool.invoke(arguments, context)).output["path"]
        == "repo://parent/link"
    )
    assert not (repo / "first/link").is_symlink()
    assert (repo / "second/link").is_symlink()
    assert (repo / "first/target").read_text() == "keep"


def test_call_keeps_its_captured_grant_but_next_call_uses_new_context(fs):
    tools, context, repo = fs
    other = repo.parent / "other"
    other.mkdir()
    args = {"path": "repo://file", "text": "old"}
    assert tools["fs__write"].paths(args, context) == {"repo": ("/file",)}
    with pytest.raises(ToolangError, match="workspace is not available: workspace"):
        tools["fs__list"].paths({"path": "workspace://"}, context)
    changed = replace(context, workspaces={"repo": other, "new": repo})
    assert (
        asyncio.run(tools["fs__write"].invoke(args, context)).output["path"]
        == "repo://file"
    )
    assert (repo / "file").read_text() == "old"
    assert not (other / "file").exists()
    asyncio.run(tools["fs__write"].invoke({**args, "text": "new"}, changed)).output
    assert (other / "file").read_text() == "new"
    assert (
        asyncio.run(tools["fs__read"].invoke({"path": "repo://file"}, context)).output[
            "text"
        ]
        == "old"
    )
    assert (
        asyncio.run(tools["fs__read"].invoke({"path": "repo://file"}, changed)).output[
            "text"
        ]
        == "new"
    )
    with pytest.raises(ToolangError, match="not available"):
        tools["fs__write"].paths(args, replace(context, workspaces={}))


def test_plain_paths_need_cwd_but_explicit_paths_do_not(fs):
    tools, context, repo = fs
    with pytest.raises(
        ToolangError, match="relative path requires a current workspace"
    ):
        _invoke(fs, "write", path="file", text="bad")
    assert (
        _invoke(fs, "write", path="repo://file", text="done")["path"] == "repo://file"
    )
    assert (repo / "file").read_text() == "done"
    with pytest.raises(ToolangError, match="path, not workspace"):
        _invoke(fs, "write", path="file", workspace="repo", text="bad")
    with pytest.raises(ToolangError, match="requires a current workspace"):
        tools["shell__execute"].paths({"command": "true"}, context)
    with pytest.raises(ToolangError, match="per-call cwd is unavailable"):
        tools["shell__execute"].paths({"cwd": str(repo), "command": "true"}, context)


@pytest.mark.parametrize(
    "name", ["list", "glob", "read", "write", "append", "mkdir", "stat", "remove"]
)
def test_home_is_not_an_implicit_filesystem_grant(fs, name):
    with pytest.raises(ToolangError, match="outside configured workspaces"):
        _invoke(fs, name, path=str(fs[1].home), text="bad")
    assert list(fs[1].home.iterdir()) == []


def test_uri_errors_identify_the_logical_path(fs):
    with pytest.raises(ToolangError) as exc:
        _invoke(fs, "read", path="repo://missing")
    assert "repo://missing" in str(exc.value)
    assert str(fs[2]) not in str(exc.value)


@pytest.mark.parametrize(
    "value", ["repo://a//b", "repo://a/./b", "repo://a/../b", "repo://a/"]
)
def test_durable_cwd_rejects_noncanonical_segments(value):
    with pytest.raises(ToolangError, match="noncanonical working location"):
        parse_cwd(value)


def test_absolute_paths_choose_deepest_root_while_named_paths_keep_identity(tmp_path):
    repo = tmp_path / "repo"
    sdk = repo / "sdk"
    sdk.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    context = ToolContext(home, home, workspaces={"repo": repo, "sdk": sdk})
    write = load_tools(queries=("fs/*",))["fs__write"]
    for path, name, relative in [
        (str(sdk / "result"), "sdk", "/result"),
        (str(sdk / "result"), "sdk", "/result"),
        ("repo://sdk/result", "repo", "/sdk/result"),
        ("sdk://result", "sdk", "/result"),
    ]:
        assert write.paths({"path": path, "text": "done"}, context) == {
            name: (relative,)
        }
    assert write.paths(
        {"path": "sdk/result", "text": "done"}, replace(context, cwd="repo://")
    ) == {"repo": ("/sdk/result",)}


@pytest.mark.parametrize(
    "path",
    ["repo://src%2Ffile", "repo://src%2ffile", "file:///%2Ftmp"],
)
def test_decoding_cannot_change_uri_path_segments(fs, path):
    with pytest.raises(ToolangError, match="encoded path separator"):
        _invoke(fs, "read", path=path)


def test_slash_root_is_an_explicit_grant_but_nested_root_wins_absolute_paths(tmp_path):
    from pathlib import Path
    from toolang.base.utils.workspace_paths import resolve_input_path
    from toolang.state.config import ConfiguredWorkspaces

    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    configured = ConfiguredWorkspaces.parse(
        f'[workspaces]\nall = "/"\nrepo = "{repo}"\n'
    )
    assert configured == {"all": "/", "repo": str(repo)}
    context = ToolContext(
        home, home, workspaces={"all": Path("/"), "repo": repo}, cwd="all://"
    )
    _path, name, relative = resolve_input_path(str(repo / "file"), context)
    assert (name, relative) == ("repo", "/file")
    _path, name, relative = resolve_input_path(str(tmp_path / "outside"), context)
    assert name == "all"
    assert relative == str(tmp_path / "outside")


def test_name_uris_are_workspace_paths_and_file_url_forms_are_not_special(tmp_path):
    from toolang.base.utils.workspace_paths import resolve_input_path

    file_root, https_root = tmp_path / "file", tmp_path / "https"
    file_root.mkdir()
    https_root.mkdir()
    context = ToolContext(
        tmp_path,
        tmp_path,
        workspaces={"file": file_root, "https": https_root},
        cwd="https://",
    )
    assert resolve_input_path("file://data", context) == (
        file_root / "data",
        "file",
        "/data",
    )
    assert resolve_input_path("https://data", context) == (
        https_root / "data",
        "https",
        "/data",
    )
    with pytest.raises(ToolangError, match="workspace path must be relative"):
        resolve_input_path("file:///tmp", context)


def test_path_guidance_teaches_only_accepted_workspace_references(fs):
    """Regression: guidance taught `:<workspace>://<path>`, which the resolver rejects."""

    tools, context, _repo = fs
    descriptions = {
        name: tool.definition().description
        for name, tool in tools.items()
        if name.startswith("fs__")
    }
    assert descriptions
    for description in descriptions.values():
        assert "name://path such as repo://src/main.py" in description
        assert ":" + "<workspace>" not in description
    assert resolve_input_path("repo://src/main.py", context)[1:] == (
        "repo",
        "/src/main.py",
    )
    with pytest.raises(ToolangError, match="invalid path reference"):
        resolve_input_path(":repo://src/main.py", context)
