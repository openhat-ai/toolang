"""The complete built-in catalog keeps input targets across lifecycle states."""

from pathlib import Path

import pytest

from toolang.base.types.run import ToolCall
from toolang.base.types.tool import ToolResult
from toolang.execution.executor.steps.tool import _tool_summary, _tool_summary_context
from toolang.plugin.toolsets.loading import load_tools

# ref, supplied input, base/running/success verbs, object and input target
CATALOG = [
    ("state/list", {}, ("list", "Listing", "Listed"), "agent files"),
    (
        "state/get",
        {"key": "agent.too"},
        ("read", "Reading", "Read"),
        "agent file: agent.too",
    ),
    (
        "state/create",
        {"key": "agent.too"},
        ("create", "Creating", "Created"),
        "agent file: agent.too",
    ),
    (
        "state/update",
        {"key": "agent.too"},
        ("update", "Updating", "Updated"),
        "agent file: agent.too",
    ),
    (
        "state/delete",
        {"key": "agent.too"},
        ("delete", "Deleting", "Deleted"),
        "agent file: agent.too",
    ),
    ("state/sync", {}, ("sync", "Syncing", "Synced"), "agent state"),
    (
        "fs/list",
        {"path": "repo://src"},
        ("list", "Listing", "Listed"),
        "files: repo://src",
    ),
    (
        "fs/read",
        {"path": "repo://src"},
        ("read", "Reading", "Read"),
        "file: repo://src",
    ),
    (
        "fs/write",
        {"path": "repo://src"},
        ("write", "Writing", "Wrote"),
        "file: repo://src",
    ),
    (
        "fs/append",
        {"path": "repo://src"},
        ("append to", "Appending to", "Appended to"),
        "file: repo://src",
    ),
    (
        "fs/glob",
        {"path": "repo://src", "pattern": "*.py"},
        ("match", "Matching", "Matched"),
        "files: *.py in repo://src",
    ),
    (
        "fs/stat",
        {"path": "repo://src"},
        ("inspect", "Inspecting", "Inspected"),
        "path: repo://src",
    ),
    (
        "fs/mkdir",
        {"path": "repo://src"},
        ("create", "Creating", "Created"),
        "directory: repo://src",
    ),
    (
        "fs/remove",
        {"path": "repo://src"},
        ("remove", "Removing", "Removed"),
        "path: repo://src",
    ),
    ("shell/execute", {"command": "ls"}, ("", "Running:", "Ran:"), "“ls”"),
    (
        "web/search",
        {"query": "python"},
        ("search:", "Searching:", "Searched:"),
        "“python”",
    ),
    ("history/read_threads", {}, ("read", "Reading", "Read"), "threads"),
    (
        "history/read_runs",
        {"thread": "chat_a"},
        ("read", "Reading", "Read"),
        "runs: chat_a",
    ),
    (
        "history/read_steps",
        {"run": "run_a"},
        ("read", "Reading", "Read"),
        "steps: run_a",
    ),
    (
        "history/read_output",
        {"run": "run_a"},
        ("read", "Reading", "Read"),
        "run output: run_a",
    ),
    ("runtime/runnables", {}, ("discover", "Discovering", "Discovered"), "runnables"),
    (
        "runtime/chdir",
        {"path": "repo://src"},
        ("change", "Changing", "Changed"),
        "workdir: repo://src",
    ),
    (
        "runtime/run",
        {"runnable": "worker"},
        ("complete", "Running", "Completed"),
        "child run: worker",
    ),
    (
        "runtime/spawn",
        {"runnable": "worker"},
        ("spawn", "Spawning", "Spawned"),
        "new run: worker",
    ),
    (
        "runtime/await",
        {"target": "run_a"},
        ("receive", "Waiting for", "Received"),
        "run result: run_a",
    ),
    (
        "runtime/exec",
        {"runnable": "worker"},
        ("switch to", "Switching to", "Switched to"),
        "runnable: worker",
    ),
    (
        "runtime/pick",
        {"kind": "skill", "ref": "skill/testing"},
        ("load", "Loading", "Loaded"),
        "guidance: skill/testing",
    ),
    (
        "runtime/honor",
        {"paths": [{"workspace": "repo", "path": "/src"}]},
        ("load", "Loading", "Loaded"),
        "rules for repo://src",
    ),
    ("runtime/compact", {}, ("compact", "Compacting", "Compacted"), "thread history"),
    (
        "service/start_bridge",
        {"service": "github"},
        ("start", "Starting", "Started"),
        "bridge: github",
    ),
    (
        "service/stop_bridge",
        {"service": "github"},
        ("stop", "Stopping", "Stopped"),
        "connection: github",
    ),
    (
        "service/init",
        {"service": "github"},
        ("initialize", "Initializing", "Initialized"),
        "session: github",
    ),
    (
        "service/start_auth",
        {"service": "github"},
        ("start", "Starting", "Started"),
        "authorization: github",
    ),
    (
        "service/complete_auth",
        {"service": "github"},
        ("complete", "Completing", "Completed"),
        "authorization: github",
    ),
    (
        "service/list_tools",
        {"service": "github"},
        ("list", "Listing", "Listed"),
        "tools: github",
    ),
    (
        "service/call_tool",
        {"service": "github", "tool_name": "issues"},
        ("call", "Calling", "Called"),
        "tool: github/issues",
    ),
    (
        "service/list_resources",
        {"service": "github"},
        ("list", "Listing", "Listed"),
        "resources: github",
    ),
    (
        "service/list_resource_templates",
        {"service": "github"},
        ("list", "Listing", "Listed"),
        "resource templates: github",
    ),
    (
        "service/read_resource",
        {"service": "github", "resource_uri": "repo://readme"},
        ("read", "Reading", "Read"),
        "resource: github repo://readme",
    ),
    (
        "service/list_prompts",
        {"service": "github"},
        ("list", "Listing", "Listed"),
        "prompts: github",
    ),
    (
        "service/get_prompt",
        {"service": "github", "prompt_name": "review"},
        ("get", "Getting", "Got"),
        "prompt: github/review",
    ),
    ("convo/team", {}, ("list", "Listing", "Listed"), "team members"),
    (
        "convo/list",
        {"name": "dev", "members": ["alice", "bob"]},
        ("list", "Listing", "Listed"),
        "conversations: dev; with alice, bob",
    ),
    (
        "convo/peek",
        {"target": "alice"},
        ("read", "Reading", "Read"),
        "conversation messages: alice",
    ),
    (
        "convo/write",
        {"target": "alice"},
        ("write", "Writing", "Wrote"),
        "message: alice",
    ),
    (
        "convo/create",
        {"name": "dev"},
        ("create", "Creating", "Created"),
        "conversation: dev",
    ),
    (
        "convo/rename",
        {"conversation": "gc_00000000", "name": "dev"},
        ("rename", "Renaming", "Renamed"),
        'conversation: gc_00000000 → "dev"',
    ),
    (
        "convo/join",
        {"conversation": "gc_00000000"},
        ("join", "Joining", "Joined"),
        "conversation: gc_00000000",
    ),
    (
        "convo/leave",
        {"conversation": "gc_00000000"},
        ("leave", "Leaving", "Left"),
        "conversation: gc_00000000",
    ),
]


@pytest.fixture(scope="module")
def tools():
    return load_tools()


def test_catalog_covers_every_registered_leaf_and_no_old_aliases(tools):
    assert len(CATALOG) == len(tools) == 49
    assert {ref.replace("/", "__") for ref, *_ in CATALOG} == set(tools)
    assert not any(name.startswith(("me__", "msg__", "_toolang__")) for name in tools)


@pytest.mark.parametrize("ref,args,verbs,label", CATALOG, ids=[r[0] for r in CATALOG])
@pytest.mark.parametrize("status", ["running", "succeeded", "failed", "canceled"])
def test_catalog_lifecycle_and_input_targets(
    tools, ref, args, verbs, label, status, monkeypatch
):
    def no_io(*args, **kwargs):
        raise AssertionError("summary performed filesystem IO")

    monkeypatch.setattr(Path, "resolve", no_io)
    monkeypatch.setattr(Path, "read_bytes", no_io)
    name = ref.replace("/", "__")
    call = ToolCall(
        "call",
        "provider",
        name,
        {
            **args,
            "message": "private body",
            "content": "private content",
            "token": "secret",
            "input": {"hidden": "payload"},
        },
    )
    result = (
        None
        if status == "running"
        else ToolResult(
            {
                "conversation": "dm_other",
                "run_id": "run_other",
                "text": "private output",
                "error": "nested error",
            },
            error="raw failure" if status == "failed" else None,
        )
    )
    expected = (
        f"{verbs[1]} {label}"
        if status in {"running", "canceled"}
        else f"{verbs[2]} {label}"
        if status == "succeeded"
        else f"Failed to {verbs[0]} {label}"
    )
    if status == "canceled":
        expected = (
            f"Canceled: {label}" if ref == "shell/execute" else "Canceled: " + expected
        )
    if status == "failed" and ref == "shell/execute":
        expected = f"Failed: {label}"
    assert (
        _tool_summary(_tool_summary_context(call, tools[name]), status, result)
        == expected
    )


@pytest.mark.parametrize(
    "args,output,expected",
    [
        (
            {"name": "dev", "participants": ["agent:alice", "human:bob"]},
            {"id": "gc_00000000"},
            "Created conversation: dev with agent:alice, human:bob (gc_00000000)",
        ),
        (
            {"participants": ["agent:alice", "human:bob"]},
            {},
            "Created conversation: with agent:alice, human:bob",
        ),
        ({}, {"id": "dm_00000000"}, "Created conversation: dm_00000000"),
        ({}, {}, "Created conversation"),
    ],
)
def test_created_keeps_label_and_supplements_id(tools, args, output, expected):
    assert tools["convo__create"].summary(args, ToolResult(output)) == expected


@pytest.mark.parametrize(
    "previous,name,expected",
    [
        ("old", "new", '"old" → "new"'),
        (None, "new", 'unnamed → "new"'),
        ("old", None, '"old" → unnamed'),
    ],
)
def test_renamed_includes_verified_previous_name(tools, previous, name, expected):
    assert (
        tools["convo__rename"].summary(
            {"conversation": "gc_00000000", "name": name},
            ToolResult({"previous_name": previous}),
        )
        == f"Renamed conversation: {expected} (gc_00000000)"
    )


@pytest.mark.parametrize(
    "status,expected",
    [
        ("running", "Starting"),
        ("succeeded", "Started"),
        ("failed", "Failed to start"),
        ("canceled", "Canceled: Starting"),
    ],
)
def test_async_run_preserves_runnable(tools, status, expected):
    call = ToolCall(
        "call", "provider", "runtime__run", {"runnable": "worker", "async": True}
    )
    result = (
        None
        if status == "running"
        else ToolResult(
            {"id": "run_other"}, error="failure" if status == "failed" else None
        )
    )
    assert (
        _tool_summary(_tool_summary_context(call, tools[call.name]), status, result)
        == f"{expected} child run: worker"
    )
