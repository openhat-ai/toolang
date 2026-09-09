"""Tool wording depends only on call/result data, not live filesystem state."""

from pathlib import Path

import pytest

from toolang.base.types.tool import ToolResult
from toolang.execution.tools.runtime import RuntimeToolset
from toolang.plugin.toolsets.filesystem import FilesystemToolset
from toolang.plugin.toolsets.history import HistoryToolset
from toolang.plugin.toolsets.shell import ShellToolset
from toolang.plugin.toolsets.web_search import WebSearchToolset


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "workspace://repo/src/file.txt"},
        {"workspace": "repo", "path": "/src/file.txt"},
        {"workspace": "repo", "path": "src/file.txt"},
    ],
)
@pytest.mark.parametrize(
    "status,expected",
    [
        ("running", "Reading repo:/src/file.txt..."),
        ("succeeded", "Read repo:/src/file.txt"),
        ("failed", "Failed to read repo:/src/file.txt"),
    ],
)
def test_workspace_descriptions_do_not_resolve_paths(
    arguments, status, expected, monkeypatch
):
    def no_io(*args, **kwargs):
        raise AssertionError("description must not access the filesystem")

    monkeypatch.setattr(Path, "resolve", no_io)
    monkeypatch.setattr(Path, "read_bytes", no_io)
    tool = FilesystemToolset({}).tools()["read"]
    assert (
        tool.summary(
            arguments,
            None
            if status == "running"
            else ToolResult(
                {"text": "do not display results"},
                error="failed" if status == "failed" else None,
            ),
        )
        == expected
    )


@pytest.mark.parametrize(
    "arguments,target",
    [
        ({"path": "workspace://"}, "workspaces"),
        ({"path": "workspace://repo"}, "repo:/"),
        ({"path": "workspace://repo/"}, "repo:/"),
        ({"workspace": "repo"}, "repo:/"),
        ({"workspace": "repo", "path": "."}, "repo:/"),
        ({"path": "workspace://repo/a%20file"}, "repo:/a file"),
    ],
)
def test_workspace_root_and_catalog_descriptions(arguments, target):
    tool = FilesystemToolset({}).tools()["list"]
    assert tool.summary(arguments, ToolResult()) == f"Listed {target}"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("write", "Wrote repo:/file"),
        ("append", "Appended to repo:/file"),
        ("glob", "Matched *.py in repo:/file"),
        ("stat", "Inspected repo:/file"),
        ("mkdir", "Created directory repo:/file"),
        ("remove", "Removed repo:/file"),
    ],
)
def test_fs_verbs(name, expected):
    assert (
        FilesystemToolset({})
        .tools()[name]
        .summary(
            {"workspace": "repo", "path": "file", "pattern": "*.py"},
            ToolResult(),
        )
        == expected
    )


@pytest.mark.parametrize(
    "status,prefix",
    [
        ("running", "Running"),
        ("succeeded", "Ran"),
        ("failed", "Failed to run"),
    ],
)
def test_shell_describes_command_not_output_or_exit_status(status, prefix):
    text = (
        ShellToolset({})
        .tools()["execute"]
        .summary(
            {"command": "echo hello"},
            None
            if status == "running"
            else ToolResult(
                {"stdout": "not shown", "ok": False, "exit_code": 1},
                error="failed" if status == "failed" else None,
            ),
        )
    )
    assert text == f"{prefix} “echo hello”" + ("..." if status == "running" else "")


@pytest.mark.parametrize(
    "status,expected",
    [
        ("running", "Searching for “abc”..."),
        ("succeeded", "Searched for “abc”"),
        ("failed", "Failed to search for “abc”"),
    ],
)
def test_web_search_describes_query_without_running_search(
    status, expected, monkeypatch
):
    def no_search(*args, **kwargs):
        raise AssertionError("summary must not perform a search")

    monkeypatch.setattr("toolang.plugin.toolsets.web_search._run_search", no_search)
    tool = WebSearchToolset({}).tools()["search"]
    result = (
        None
        if status == "running"
        else ToolResult(
            {"results": [{"title": "Do not display result content"}]},
            error="unavailable" if status == "failed" else None,
        )
    )
    assert tool.summary({"query": "abc", "top_k": 5}, result) == expected


@pytest.mark.parametrize(
    "name,arguments,target",
    [
        ("read_threads", {}, "threads"),
        ("read_runs", {}, "runs"),
        ("read_runs", {"thread": "thread_abc"}, "runs from thread_abc"),
        ("read_steps", {"run": "run_abc"}, "steps from run_abc"),
        ("read_output", {"run": "run_abc"}, "output from run_abc"),
        ("read_threads", {"cursor": "opaque-token"}, "more threads"),
        ("read_runs", {"cursor": "opaque-token"}, "more runs"),
        ("read_steps", {"cursor": "opaque-token"}, "more steps"),
    ],
)
@pytest.mark.parametrize(
    "status,prefix",
    [("running", "Reading"), ("succeeded", "Read"), ("failed", "Failed to read")],
)
def test_history_describes_the_target_without_displaying_cursors_or_records(
    name, arguments, target, status, prefix
):
    result = (
        None
        if status == "running"
        else ToolResult(
            {"records": ["Do not display history contents"]},
            error="unavailable" if status == "failed" else None,
        )
    )
    summary = HistoryToolset().tools()[name].summary(arguments, result)
    assert summary == f"{prefix} {target}" + ("..." if status == "running" else "")


def test_honor_only_describes_rule_files_when_result_supplies_them():
    tool = RuntimeToolset().tools()["honor"]
    arguments = {"paths": [{"workspace": "repo", "path": "/src/file"}]}
    assert tool.summary(arguments) == "Loading rules..."
    assert tool.summary(arguments, ToolResult(error="failed")) == "Failed to load rules"
    output = {
        "controls": [
            {
                "ref": "run_x@1",
                "revision": "0",
                "target": {
                    "kind": "rules",
                    "workspace": "repo",
                    "path": "/src/AGENTS.md",
                },
            }
        ]
    }
    assert (
        tool.summary(arguments, ToolResult(output))
        == "Loaded rules: repo:/src/AGENTS.md"
    )


@pytest.mark.parametrize("kind", ["skill", "service"])
@pytest.mark.parametrize("scope", ["root", "home", "here", "inline"])
@pytest.mark.parametrize("status", ["running", "succeeded", "failed"])
def test_pick_uses_a_display_label_without_changing_the_resource_ref(
    kind, scope, status, monkeypatch
):
    def no_io(*args, **kwargs):
        raise AssertionError("summary must not read resource files")

    monkeypatch.setattr(Path, "read_bytes", no_io)
    arguments = {"kind": kind, "ref": f"{scope}://{kind}s/testing"}
    result = (
        None
        if status == "running"
        else ToolResult(error="unavailable" if status == "failed" else None)
    )
    wording = {
        "running": f"Loading guidance: {kind}/testing...",
        "succeeded": f"Loaded guidance: {kind}/testing",
        "failed": f"Failed to load guidance: {kind}/testing",
    }
    assert (
        RuntimeToolset().tools()["pick"].summary(arguments, result) == wording[status]
    )
    assert arguments == {"kind": kind, "ref": f"{scope}://{kind}s/testing"}


def test_pick_keeps_remote_resource_identity_in_its_label():
    arguments = {"kind": "skill", "ref": "https://example.com/team/testing"}
    assert RuntimeToolset().tools()["pick"].summary(arguments, ToolResult()) == (
        "Loaded guidance: skill/https://example.com/team/testing"
    )
