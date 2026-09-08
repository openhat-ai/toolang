"""Tool wording depends only on call/result data, not live filesystem state."""

from pathlib import Path

import pytest

from toolang.base.types.tool import ToolResult
from toolang.execution.tools.runtime import RuntimeToolset
from toolang.plugin.toolsets.filesystem import FilesystemToolset
from toolang.plugin.toolsets.shell import ShellToolset


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
        ("running", "Reading [repo] src/file.txt..."),
        ("succeeded", "Read [repo] src/file.txt"),
        ("failed", "Failed to read [repo] src/file.txt"),
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
        ({"path": "workspace://repo"}, "[repo] /"),
        ({"path": "workspace://repo/"}, "[repo] /"),
        ({"workspace": "repo"}, "[repo] /"),
        ({"workspace": "repo", "path": "."}, "[repo] /"),
        ({"path": "workspace://repo/a%20file"}, "[repo] a file"),
    ],
)
def test_workspace_root_and_catalog_descriptions(arguments, target):
    tool = FilesystemToolset({}).tools()["list"]
    assert tool.summary(arguments, ToolResult()) == f"Listed {target}"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("write", "Wrote [repo] file"),
        ("append", "Appended to [repo] file"),
        ("glob", "Matched *.py in [repo] file"),
        ("stat", "Inspected [repo] file"),
        ("mkdir", "Created directory [repo] file"),
        ("remove", "Removed [repo] file"),
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


def test_honor_only_describes_rule_files_when_result_supplies_them():
    tool = RuntimeToolset().tools()["honor"]
    arguments = {"paths": [{"workspace": "repo", "path": "/src/file"}]}
    assert tool.summary(arguments) == "Reloading workspace rules..."
    assert (
        tool.summary(arguments, ToolResult(error="failed"))
        == "Failed to reload workspace rules"
    )
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
        == "Reloaded workspace rules: [repo] src/AGENTS.md"
    )
