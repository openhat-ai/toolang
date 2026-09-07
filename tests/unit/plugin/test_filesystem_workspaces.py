"""Workspace URI tools use captured grants and return reusable logical paths."""

import asyncio
from dataclasses import replace

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.tool import ToolContext
from toolang.base.utils.function_tools import prepare_tool
from toolang.base.utils.workspace_paths import parse_workspace_uri, workspace_uri
from toolang.plugin.toolsets.loading import load_tools


@pytest.fixture
def fs(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    context = ToolContext(
        "run_test",
        home,
        home,
        home,
        workspaces={"repo": repo, "unavailable": tmp_path / "missing"},
    )
    return load_tools(queries=("fs/*", "shell/*")), context, repo


def _invoke(fs, name, **arguments):
    tools, context, _repo = fs
    return asyncio.run(tools[f"fs__{name}"].invoke(arguments, context))


def _invoke_prepared(preparation):
    async def invoke():
        return await preparation.invoke()

    return asyncio.run(invoke())


def test_all_filesystem_operations_and_uri_results(fs):
    _tools, _context, repo = fs
    listing = _invoke(fs, "list", path="workspace://")
    assert listing == {
        "path": "workspace://",
        "entries": [
            {"name": "repo", "path": "workspace://repo/", "available": True},
            {
                "name": "unavailable",
                "path": "workspace://unavailable/",
                "available": False,
            },
        ],
    }
    assert (
        _invoke(fs, "mkdir", path="workspace://repo/src")["path"]
        == "workspace://repo/src"
    )
    uri = "workspace://repo/src/a%20%23%3F%25%E4%B8%AD.txt"
    assert _invoke(fs, "write", path=uri, text="first")["path"] == uri
    assert _invoke(fs, "append", path=uri, text=" second")["path"] == uri
    assert _invoke(fs, "read", path=uri)["text"] == "first second"
    assert _invoke(fs, "stat", path=uri)["is_file"]
    listed = _invoke(fs, "list", path="workspace://repo/src")
    assert listed["entries"] == [{"name": "a #?%中.txt", "path": uri, "is_dir": False}]
    assert _invoke(fs, "glob", path="workspace://repo", pattern="**/*.txt")[
        "matches"
    ] == [uri]
    assert _invoke(
        fs, "glob", path="workspace://repo", pattern="*.txt", recursive=True
    )["matches"] == [uri]
    assert (
        _invoke(fs, "read", path=listed["entries"][0]["path"])["text"] == "first second"
    )
    _invoke(fs, "remove", path=uri)
    assert not _invoke(fs, "stat", path=uri)["exists"]
    _invoke(fs, "remove", path="workspace://repo/src", recursive=True)
    assert list(repo.iterdir()) == []


@pytest.mark.parametrize(
    "value",
    [
        "workspace:/repo/file",
        "workspace:///file",
        "workspace://Repo/file",
        "workspace://repo:80/file",
        "workspace://user@repo/file",
        "workspace://repo/file?query",
        "workspace://repo/file#fragment",
        "workspace://repo/file%",
        "workspace://repo/file%GG",
        "workspace://repo/%FF",
        "workspace://repo/%00",
        "runspace://coop/file",
        "WORKSPACE://repo/file",
    ],
)
def test_invalid_uri_never_falls_back_to_a_home_path(fs, value):
    with pytest.raises(ToolangError):
        _invoke(fs, "write", path=value, text="bad")
    assert list(fs[1].home.iterdir()) == []


def test_uri_decodes_once_and_keeps_host_container_identity(fs):
    assert parse_workspace_uri("workspace://repo/%252e%252e") == ("repo", "/%2e%2e")
    assert workspace_uri("repo", "/ #?%中") == "workspace://repo/%20%23%3F%25%E4%B8%AD"
    _invoke(fs, "write", path="workspace://repo/%252e%252e", text="literal")
    assert (fs[2] / "%2e%2e").read_text() == "literal"
    assert _invoke(fs, "stat", path="workspace://repo")["path"] == "workspace://repo/"


@pytest.mark.parametrize(
    "path", ["workspace://repo/../secret", "workspace://repo/%2e%2e/secret"]
)
def test_parent_traversal_cannot_escape(fs, path):
    with pytest.raises(ToolangError, match="escapes workspace"):
        _invoke(fs, "write", path=path, text="bad")


@pytest.mark.parametrize(
    "name", ["read", "write", "append", "stat", "mkdir", "remove", "glob"]
)
def test_namespace_root_is_only_for_listing(fs, name):
    with pytest.raises(ToolangError, match="only fs.list"):
        _invoke(fs, name, path="workspace://", text="bad")


def test_grants_must_exist_and_root_removal_is_rejected(fs):
    for name in ("unknown", "unavailable"):
        with pytest.raises(ToolangError, match="not available"):
            _invoke(fs, "write", path=f"workspace://{name}/file", text="bad")
    assert not fs[1].workspaces["unavailable"].exists()
    with pytest.raises(ToolangError, match="cannot remove a workspace root"):
        _invoke(fs, "remove", path="workspace://repo/", recursive=True)
    with pytest.raises(ToolangError, match="cannot be combined"):
        _invoke(fs, "read", path="workspace://repo/file", workspace="repo")


def test_symlinks_and_glob_cannot_escape_workspace(fs):
    tools, context, repo = fs
    outside = context.home / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    (repo / "escape").symlink_to(outside, target_is_directory=True)
    for name, arguments in [
        ("read", {"path": "workspace://repo/escape/secret"}),
        ("write", {"path": "workspace://repo/escape/new", "text": "bad"}),
        ("list", {"path": "workspace://repo/"}),
        ("glob", {"path": "workspace://repo/", "pattern": "escape/*"}),
        ("glob", {"path": "workspace://repo/", "recursive": True}),
    ]:
        with pytest.raises(ToolangError, match="escapes workspace"):
            asyncio.run(tools[f"fs__{name}"].invoke(arguments, context))
    assert not (outside / "new").exists()


@pytest.mark.parametrize("pattern", ["../*", "/tmp/*", "a/../../*", ""])
def test_glob_patterns_stay_in_the_selected_directory(fs, pattern):
    with pytest.raises(ToolangError, match="glob pattern"):
        _invoke(fs, "glob", path="workspace://repo/", pattern=pattern)


def test_internal_aliases_and_parent_components_keep_path_meaning(fs):
    _tools, _context, repo = fs
    (repo / "actual/nested").mkdir(parents=True)
    (repo / "link").symlink_to(repo / "actual/nested", target_is_directory=True)
    _invoke(fs, "write", path="workspace://repo/link/../file", text="correct")
    assert (repo / "actual/file").read_text() == "correct"
    assert not (repo / "file").exists()
    _invoke(fs, "write", path="workspace://repo/link/note", text="alias")
    assert (
        _invoke(fs, "list", path="workspace://repo/link")["entries"][0]["path"]
        == "workspace://repo/link/note"
    )


def test_nested_workspaces_keep_the_explicit_uri_identity(fs):
    tools, context, repo = fs
    nested = repo / "sdk"
    nested.mkdir()
    context = replace(context, workspaces={"repo": repo, "sdk": nested})
    for uri, workspace, relative in [
        ("workspace://repo/sdk/file", "repo", "/sdk/file"),
        ("workspace://sdk/file", "sdk", "/file"),
    ]:
        prepared = prepare_tool(
            tools["fs__write"], {"path": uri, "text": "done"}, context
        )
        (path,) = prepared.paths
        assert (path.workspace, path.relative) == (workspace, relative)
        assert path.resolved == nested / "file"
        assert _invoke_prepared(prepared)["path"] == uri


def test_glob_normalizes_directory_patterns_without_following_aliases(fs):
    _tools, _context, repo = fs
    (repo / "src/nested").mkdir(parents=True)
    (repo / "src/file").write_text("x")
    (repo / "alias").symlink_to(repo / "src", target_is_directory=True)
    assert _invoke(fs, "glob", path="workspace://repo/", pattern="./src/*/")[
        "matches"
    ] == ["workspace://repo/src/nested"]
    assert (
        _invoke(fs, "glob", path="workspace://repo/", pattern="alias/*")["matches"]
        == []
    )
    with pytest.raises(ToolangError, match="invalid glob pattern"):
        _invoke(fs, "glob", path="workspace://repo/", pattern="a**b")


def test_recursive_glob_ignores_unmatched_external_symlinks(fs):
    _tools, context, repo = fs
    (repo / "src").mkdir()
    (repo / "src/main.py").write_text("pass")
    outside = context.home / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("private")
    (repo / "dependencies").symlink_to(outside, target_is_directory=True)
    (repo / "interpreter").symlink_to(outside / "secret.py")

    assert _invoke(fs, "glob", path="workspace://repo/", pattern="**/*.py")[
        "matches"
    ] == ["workspace://repo/src/main.py"]


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
    assert _invoke(
        fs, "glob", path="workspace://repo/", pattern=pattern, recursive=True
    )["matches"] == ["workspace://repo/a/b/c/d/e/f/main.py"]
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

    assert _invoke(fs, "remove", path="workspace://repo/alias", recursive=True) == {
        "path": "workspace://repo/alias",
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
    _invoke(fs, "remove", path="workspace://repo/self", recursive=True)
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
        _invoke(fs, "remove", path="workspace://repo/bridge/back")
    assert target.read_text() == "keep"
    assert link.is_symlink()


def test_prepared_remove_keeps_the_parent_directory_when_an_alias_changes(fs):
    tools, context, repo = fs
    for name in ("first", "second"):
        directory = repo / name
        directory.mkdir()
        (directory / "target").write_text("keep")
        (directory / "link").symlink_to(directory / "target")
    parent = repo / "parent"
    parent.symlink_to(repo / "first", target_is_directory=True)
    prepared = prepare_tool(
        tools["fs__remove"], {"path": "workspace://repo/parent/link"}, context
    )
    parent.unlink()
    parent.symlink_to(repo / "second", target_is_directory=True)
    assert _invoke_prepared(prepared)["path"] == "workspace://repo/parent/link"
    assert not (repo / "first/link").is_symlink()
    assert (repo / "second/link").is_symlink()
    assert (repo / "first/target").read_text() == "keep"


def test_prepared_call_keeps_its_grant_but_next_call_uses_new_context(fs):
    tools, context, repo = fs
    other = repo.parent / "other"
    other.mkdir()
    args = {"path": "workspace://repo/file", "text": "old"}
    prepared = prepare_tool(tools["fs__write"], args, context)
    listing = prepare_tool(tools["fs__list"], {"path": "workspace://"}, context)
    changed = replace(context, workspaces={"repo": other, "new": repo})
    assert _invoke_prepared(prepared)["path"] == "workspace://repo/file"
    assert (repo / "file").read_text() == "old"
    assert not (other / "file").exists()
    asyncio.run(tools["fs__write"].invoke({**args, "text": "new"}, changed))
    assert (other / "file").read_text() == "new"
    assert [e["name"] for e in _invoke_prepared(listing)["entries"]] == [
        "repo",
        "unavailable",
    ]
    assert [
        e["name"]
        for e in asyncio.run(
            tools["fs__list"].invoke({"path": "workspace://"}, changed)
        )["entries"]
    ] == ["new", "repo"]
    with pytest.raises(ToolangError, match="not available"):
        prepare_tool(tools["fs__write"], args, replace(context, workspaces={}))


def test_plain_paths_require_a_workspace_and_shell_is_unchanged(fs):
    tools, context, repo = fs
    with pytest.raises(ToolangError, match="specify workspace"):
        _invoke(fs, "write", path="file", text="home")
    with pytest.raises(ToolangError, match="specify workspace"):
        _invoke(fs, "write", path=str(repo / "file"), text="bad")
    assert (
        _invoke(fs, "write", path="file", workspace="repo", text="done")["path"]
        == "workspace://repo/file"
    )
    assert (repo / "file").read_text() == "done"
    with pytest.raises(ToolangError, match="escapes agent home"):
        prepare_tool(
            tools["shell__execute"], {"cwd": str(repo), "command": "true"}, context
        )


@pytest.mark.parametrize(
    "name", ["list", "glob", "read", "write", "append", "mkdir", "stat", "remove"]
)
def test_home_is_not_an_implicit_filesystem_grant(fs, name):
    with pytest.raises(ToolangError, match="agent home is not accessible"):
        _invoke(fs, name, path=str(fs[1].home), text="bad")
    assert list(fs[1].home.iterdir()) == []


def test_uri_errors_identify_the_logical_path(fs):
    with pytest.raises(ToolangError) as exc:
        _invoke(fs, "read", path="workspace://repo/missing")
    assert "workspace://repo/missing" in str(exc.value)
    assert str(fs[2]) not in str(exc.value)
