"""The me toolset reads and atomically replaces current home files."""

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

import pytest

from toolang.base.types.tool import ToolContext
from toolang.common.layout import AgentLayout
from toolang.execution.tools.me import create_toolset
from toolang.execution.tools.me.types import MeToolContext
from toolang.state.errors import StatePreparationError
from toolang.state.prepare import prepare_agent_state
from toolang.state.watcher import StateWatcher


@pytest.fixture
def context(tmp_path: Path) -> MeToolContext:
    layout = AgentLayout.resident(tmp_path / "toolang", "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("agic alice():\n  Hello.\n")
    return MeToolContext(home=layout.home, room=layout.tool_room("me"), layout=layout)


def invoke(context: ToolContext, operation: str, **arguments):
    return asyncio.run(create_toolset({}).tools()[operation].invoke(arguments, context))


def read(context, key):
    result = invoke(context, "get", key=key)
    assert result.error is None, result.output
    return result.output


def test_six_closed_me_schemas():
    tools = create_toolset({}).tools()
    assert tuple(tools) == ("list", "get", "create", "update", "delete", "loaded")
    for name, tool in tools.items():
        schema = tool.definition().parameters
        assert schema["additionalProperties"] is False
        assert {"kind", "revision", "root", "home", "scope"}.isdisjoint(
            schema["properties"]
        )
        assert ("if_digest" in schema["required"]) == (name in {"update", "delete"})
        if name in {"create", "update"}:
            assert schema["properties"]["content"]["type"] == "string"
    assert tools["list"].definition().parameters["properties"] == {}


@pytest.mark.parametrize(
    ("key", "content"),
    [
        ("config.toml", '# Keep comments\r\n[default]\r\nmodel = "test/model"\r\n'),
        ("flows/research.too", "flow:\n  pass\n"),
        ("psyches/careful.md", "Think carefully.\n"),
        ("prompts/ask.md", "Ask café.\r\n"),
        (
            "services/docs.md",
            "---\ndescription: Docs\ntransport: http\ntarget: https://example.com\n---\nUse docs.\n",
        ),
        (
            "skills/review/SKILL.md",
            "---\ndescription: Review\n---\nReview carefully.\n",
        ),
        ("skills/review/assets/notes/example.txt", "Example.\n"),
        ("skills/review/assets/notes/a:b.txt", "POSIX filename.\n"),
        ("skills/review/assets/notes/line\nbreak.txt", "POSIX filename.\n"),
        ("tasks/review.md", "---\nid: job-a\ntitle: Review\n---\nReview.\n"),
        ("chores/check.md", "---\nid: job-b\nschedule: FREQ=HOURLY\n---\nCheck.\n"),
    ],
)
def test_complete_file_crud_and_exact_bytes(context, key, content):
    created = invoke(context, "create", key=key, content=content)
    assert created.error is None, created.output
    assert created.output == {
        "key": key,
        "digest": sha256(content.encode()).hexdigest(),
    }
    item = read(context, key)
    encoded = content.encode("utf-8")
    assert item == {
        "key": key,
        "content": content,
        "encoding": "utf-8",
        "digest": sha256(encoded).hexdigest(),
        "bytes": len(encoded),
    }
    path = context.home / key
    assert path.read_bytes() == encoded
    listed = invoke(context, "list").output["files"]
    assert [entry["key"] for entry in listed] == sorted(
        entry["key"] for entry in listed
    )
    assert (
        next(entry for entry in listed if entry["key"] == key)["digest"]
        == item["digest"]
    )
    assert all("content" not in entry for entry in listed)
    duplicate = invoke(context, "create", key=key, content=content)
    assert duplicate.output["error"] == "already_exists"
    identical = invoke(
        context, "update", key=key, content=content, if_digest=item["digest"]
    )
    assert identical.output == created.output
    updated_text = content + "\n"
    updated = invoke(
        context, "update", key=key, content=updated_text, if_digest=item["digest"]
    )
    assert updated.error is None, updated.output
    assert updated.output == {
        "key": key,
        "digest": sha256(updated_text.encode()).hexdigest(),
    }
    assert read(context, key)["digest"] == updated.output["digest"]
    stale = invoke(context, "delete", key=key, if_digest=item["digest"])
    assert stale.output["error"] == "digest_mismatch"
    assert path.read_bytes() == updated_text.encode()
    deleted = invoke(context, "delete", key=key, if_digest=updated.output["digest"])
    assert deleted.error is None
    assert deleted.output == {"key": key, "digest": None}
    assert not path.exists()
    assert invoke(context, "get", key=key).output["error"] == "not_found"


def test_reads_latest_main_source_and_does_not_publish(context):
    state = prepare_agent_state(context.layout)
    original_revision = state.revision
    source = "# New café source\r\nagic alice():\r\n  Changed.\r\n"
    context.layout.program.write_bytes(source.encode())
    item = read(context, "agent.too")
    assert item["content"] == source
    assert item["digest"] != state.module_digests["agent"]
    context.layout.program.chmod(0o751)
    saved = invoke(
        context,
        "update",
        key="agent.too",
        content="agic alice():\n  Saved.\n",
        if_digest=item["digest"],
    )
    assert saved.error is None, saved.output
    assert read(context, "agent.too")["digest"] == saved.output["digest"]
    assert context.layout.program.stat().st_mode & 0o777 == 0o751
    assert state.revision == original_revision
    from toolang.state.cache import load_current_agent_revision

    assert load_current_agent_revision(context.layout) == original_revision


def test_active_run_reads_and_repairs_latest_file_without_switching_revision(tmp_path):
    from tests.support.execution_harness import (
        AsyncGate,
        ExecutionHarness,
        ScriptedModelTurn,
    )
    from toolang.base.types.message import Message, ToolResultPart
    from toolang.base.types.run import ModelCallResult, ToolCall
    from toolang.execution.types import ThreadPrefix
    from toolang.plugin.toolsets.loading import load_tools

    source = "agic inspect():\n  tools = me/*\n  Inspect.\n"
    broken = "agic ("
    repaired = "agic inspect():\n  tools = me/*\n  Repaired.\n"
    gate = AsyncGate()
    loaded_gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        tools=load_tools(queries=("me/*",)),
        responses=[
            ScriptedModelTurn(
                gate=gate,
                result=ModelCallResult(
                    tool_calls=(
                        ToolCall("read", "read", "me__get", {"key": "agent.too"}),
                    )
                ),
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "write",
                        "write",
                        "me__update",
                        {
                            "key": "agent.too",
                            "content": repaired,
                            "if_digest": sha256(broken.encode()).hexdigest(),
                        },
                    ),
                )
            ),
            ScriptedModelTurn(
                gate=loaded_gate,
                result=ModelCallResult(
                    tool_calls=(
                        ToolCall(
                            "loaded",
                            "loaded",
                            "me__loaded",
                            {
                                "receipts": [
                                    {
                                        "key": "agent.too",
                                        "digest": sha256(repaired.encode()).hexdigest(),
                                    }
                                ]
                            },
                        ),
                    )
                ),
            ),
            ModelCallResult(tool_calls=(ToolCall("list", "list", "me__list", {}),)),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            running = harness.executor.run(
                harness.run_spec(thread=thread, runnable="inspect")
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=5)
            harness.setup.layout.program.write_text(broken)
            gate.release()
            await asyncio.wait_for(loaded_gate.wait_until_entered(), timeout=5)
            published = prepare_agent_state(harness.setup.layout)
            assert published.revision != harness.state.revision
            loaded_gate.release()
            run = await asyncio.wait_for(running, timeout=5)
            assert run.status == "succeeded", run.error
            assert (
                harness.store.resolve_state_revision(run.state)
                == harness.state.revision
            )
            returned = [
                part
                for message in harness.adapter.invocations[-1].call.messages
                for part in message.parts
                if isinstance(part, ToolResultPart)
            ]
            assert len(returned) == 4
            assert all(part.error is None for part in returned)
            assert isinstance(returned[0].output, dict)
            assert isinstance(returned[1].output, dict)
            assert returned[0].output["content"] == broken
            assert returned[1].output == {
                "key": "agent.too",
                "digest": sha256(repaired.encode()).hexdigest(),
            }
            assert returned[2].output == {
                "loaded": False,
                "revision": harness.state.revision,
                "mismatches": [
                    {"key": "agent.too", "digest": sha256(source.encode()).hexdigest()}
                ],
            }
            assert returned[3].output["files"] == [
                {
                    "key": "agent.too",
                    "digest": sha256(repaired.encode()).hexdigest(),
                    "bytes": len(repaired.encode()),
                }
            ]
            persisted = [
                step.output.local.value
                for step in harness.store.list_steps(run_id=run.id)
                if step.output is not None
                and isinstance(step.output.local.value, ToolResultPart)
            ]
            assert persisted == returned
            assert harness.setup.layout.program.read_text() == repaired

    asyncio.run(scenario())


def test_failed_atomic_save_preserves_original_file_and_digest(context, monkeypatch):
    import toolang.common.files as files

    item = read(context, "agent.too")

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(files.os, "replace", fail_replace)
    result = invoke(
        context,
        "update",
        key="agent.too",
        content="replacement",
        if_digest=item["digest"],
    )
    assert result.output["error"] == "io_error"
    assert read(context, "agent.too") == item
    assert sorted(path.name for path in context.home.iterdir()) == [
        ".agent.too.lock",
        "agent.too",
    ]


def test_assets_round_trip_binary_and_do_not_get_deleted_with_skill(context):
    definition = "skills/review/SKILL.md"
    assert (
        invoke(
            context,
            "create",
            key=definition,
            content="---\ndescription: Review\n---\nReview.",
        ).error
        is None
    )
    key = "skills/review/assets/image.bin"
    encoded = b"\x00\xff\xfe\x80\r\n"
    result = invoke(
        context,
        "create",
        key=key,
        content=base64.b64encode(encoded).decode(),
        encoding="base64",
    )
    assert result.error is None, result.output
    item = read(context, key)
    assert item["encoding"] == "base64"
    assert base64.b64decode(item["content"]) == encoded
    assert item["digest"] == sha256(encoded).hexdigest()
    assert (context.home / key).read_bytes() == encoded
    deleted = invoke(
        context, "delete", key=definition, if_digest=read(context, definition)["digest"]
    )
    assert deleted.error is None
    assert (context.home / key).read_bytes() == encoded
    assert key in [item["key"] for item in invoke(context, "list").output["files"]]


@pytest.mark.parametrize(
    ("operation", "arguments", "code"),
    [
        ("list", {"kind": "flow"}, "invalid_request"),
        ("get", {"key": "agent.too", "revision": "0" * 64}, "invalid_request"),
        ("get", {}, "invalid_request"),
        (
            "update",
            {"key": "agent.too", "content": "agic:\n  Hi.\n"},
            "invalid_request",
        ),
        ("delete", {"key": "agent.too", "if_digest": None}, "invalid_request"),
        ("delete", {"key": "agent.too", "if_digest": "bad"}, "invalid_request"),
        ("delete", {"key": "agent.too", "if_digest": "0" * 64}, "digest_mismatch"),
        (
            "create",
            {"key": "prompts/a.md", "content": {"body": "hello"}},
            "invalid_request",
        ),
        (
            "create",
            {"key": "prompts/a.md", "content": "hello", "encoding": {}},
            "invalid_request",
        ),
        (
            "create",
            {"key": "skills/a/assets/x.bin", "content": "!!", "encoding": "base64"},
            "invalid_request",
        ),
    ],
)
def test_bad_requests_do_not_mutate(context, operation, arguments, code):
    before = context.layout.program.read_bytes()
    result = invoke(context, operation, **arguments)
    assert result.error is not None, result.output
    assert result.output["error"] == code, result.output
    assert context.layout.program.read_bytes() == before
    assert not (context.home / "prompts/a.md").exists()


@pytest.mark.parametrize(
    "key",
    [
        "/tmp/agent.too",
        "../agent.too",
        "home:agent.too",
        "root:psyches/a.md",
        "flows/../agent.too",
        "./agent.too",
        "flows//a.too",
        "flows/a.too/",
        "agent.too\x00",
        "flows\\a.too",
        "agents/bob/agent.too",
        ".runtime/execution.db",
        "catalog.json",
        "flows/nested/a.too",
        "skills/a/scripts/run.py",
        "archive/tasks/a.md",
        "drafts/chores/a.md",
        "skills/a/assets",
    ],
)
def test_keys_cannot_escape_or_expand_whitelist(context, key):
    result = invoke(context, "create", key=key, content="unwanted")
    assert result.error is not None
    assert result.output["error"] == "invalid_request"


def test_list_excludes_other_homes_root_resources_and_unmanaged_files(context):
    for path in (
        context.layout.root / "psyches/root.md",
        context.layout.root / "agents/bob/agent.too",
        context.home / "drafts/tasks/draft.md",
        context.home / "skills/review/scripts/run.py",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Excluded.")
    result = invoke(context, "list")
    assert result.error is None, result.output
    assert [item["key"] for item in result.output["files"]] == ["agent.too"]


def test_context_cannot_select_another_home(context):
    from dataclasses import replace

    other = context.layout.root / "agents/bob"
    other.mkdir()
    result = invoke(replace(context, home=other), "list")
    assert result.output["error"] == "io_error"
    generic = ToolContext(home=context.home, room=context.room)
    assert invoke(generic, "list").output["error"] == "invalid_request"


@pytest.mark.parametrize("writer", ["me", "filesystem"])
@pytest.mark.parametrize("source", ["agic (", "flow main:\n  run missing\n"])
def test_loader_rejects_bad_source_from_either_writer_and_recovers(
    context, writer, source
):
    state = prepare_agent_state(context.layout)
    watcher = StateWatcher(context.layout, initial_state=state)
    if writer == "me":
        result = invoke(
            context,
            "update",
            key="agent.too",
            content=source,
            if_digest=read(context, "agent.too")["digest"],
        )
        assert result.error is None, result.output
    else:
        context.layout.program.write_text(source)
    assert read(context, "agent.too")["content"] == source
    rejected = asyncio.run(watcher.refresh_result())
    assert rejected.state.revision == state.revision
    assert rejected.diagnostics
    repaired = invoke(
        context,
        "update",
        key="agent.too",
        content="agic alice():\n  Fixed.\n",
        if_digest=read(context, "agent.too")["digest"],
    )
    assert repaired.error is None, repaired.output
    accepted = asyncio.run(watcher.refresh_result())
    assert not accepted.diagnostics
    assert accepted.state.revision != state.revision


@pytest.mark.parametrize(
    ("key", "content"),
    [
        ("services/a.md", "No metadata."),
        ("skills/a/SKILL.md", "No description."),
        ("chores/a.md", "---\nschedule: invalid\n---\nCheck."),
        ("flows/bad-name.extra.too", "Not a program."),
    ],
)
def test_content_validation_is_owned_by_loaders(context, key, content):
    result = invoke(context, "create", key=key, content=content)
    assert result.error is None, result.output
    assert read(context, key)["content"] == content


def test_binary_source_is_saved_and_can_be_repaired(context):
    item = read(context, "agent.too")
    result = invoke(
        context,
        "update",
        key="agent.too",
        content="/wA=",
        encoding="base64",
        if_digest=item["digest"],
    )
    assert result.error is None, result.output
    assert context.layout.program.read_bytes() == b"\xff\0"
    item = read(context, "agent.too")
    assert item["content"] == "/wA="
    assert item["encoding"] == "base64"
    repaired = invoke(
        context,
        "update",
        key="agent.too",
        content="agic:\n  Fixed.\n",
        if_digest=item["digest"],
    )
    assert repaired.error is None, repaired.output


@pytest.mark.parametrize("broken", [b"agic (", b"\xff"])
def test_can_read_digest_and_repair_corrupt_main_source(context, broken):
    context.layout.program.write_bytes(broken)
    item = read(context, "agent.too")
    result = invoke(
        context,
        "update",
        key="agent.too",
        content="agic:\n  Repaired.\n",
        if_digest=item["digest"],
    )
    assert result.error is None, result.output


def test_flow_deletion_is_saved_and_composition_errors_belong_to_watcher(context):
    assert (
        invoke(
            context, "create", key="flows/research.too", content="flow:\n  pass\n"
        ).error
        is None
    )
    item = read(context, "agent.too")
    assert (
        invoke(
            context,
            "update",
            key="agent.too",
            content="flow main:\n  run research\n",
            if_digest=item["digest"],
        ).error
        is None
    )
    flow = read(context, "flows/research.too")
    state = prepare_agent_state(context.layout)
    watcher = StateWatcher(context.layout, initial_state=state)
    result = invoke(context, "delete", key=flow["key"], if_digest=flow["digest"])
    assert result.error is None, result.output
    assert not (context.home / flow["key"]).exists()
    rejected = asyncio.run(watcher.refresh_result())
    assert rejected.state.revision == state.revision
    assert rejected.diagnostics
    assert (context.home / ".agent.too.lock").is_file()
    assert (context.home / ".flows.lock").is_file()
    assert not (context.home / ".authored-flows.lock").exists()


@pytest.mark.parametrize(
    "content",
    [
        "[broken",
        "[allow]\ntools = 1",
        "[default]\nunexpected = true",
        "[workspaces]\na = 42",
    ],
)
def test_config_saves_invalid_content_without_projecting_original(context, content):
    original = '# Preserve original paths/comments\n[workspaces]\nrepo = "."\n[plugin.model.test]\nvalue = "unchanged"\n'
    (context.home / "config.toml").write_text(original)
    item = read(context, "config.toml")
    assert item["content"] == original
    result = invoke(
        context, "update", key="config.toml", content=content, if_digest=item["digest"]
    )
    assert result.error is None, result.output
    assert read(context, "config.toml")["content"] == content


def test_config_writes_preserve_original_fields_and_relative_paths(context):
    source = (
        '# Original\n[workspaces]\nrepo = "."\n[plugin.model.test]\nvalue = "kept"\n'
    )
    result = invoke(context, "create", key="config.toml", content=source)
    assert result.error is None, result.output
    assert read(context, "config.toml")["content"] == source
    from toolang.catalog.config import ConfiguredCaps
    from toolang.state.config import ConfiguredWorkspaces

    config = context.home / "config.toml"
    assert ConfiguredCaps(config).lock_path == ConfiguredWorkspaces(config).lock_path
    assert ConfiguredCaps(config).lock_path == context.home / ".config.toml.lock"


def test_job_reads_never_allocate_and_crud_preserves_full_text(context):
    source = "# Manual task without an id\n\nReview.\n"
    created = invoke(context, "create", key="tasks/manual.md", content=source)
    assert created.error is None, created.output
    assert read(context, "tasks/manual.md")["content"] == source
    invoke(context, "list")
    assert not context.layout.id_state.exists()
    replacement = "---\nid: task-1\n---\nReview now.\n"
    assert (
        invoke(
            context,
            "update",
            key="tasks/manual.md",
            content=replacement,
            if_digest=created.output["digest"],
        ).error
        is None
    )
    duplicate = invoke(
        context, "create", key="chores/duplicate.md", content=replacement
    )
    assert duplicate.error is None, duplicate.output
    assert read(context, "chores/duplicate.md")["content"] == replacement
    result = invoke(
        context,
        "delete",
        key="tasks/manual.md",
        if_digest=read(context, "tasks/manual.md")["digest"],
    )
    assert result.error is None
    assert not (context.home / "archive").exists()


@pytest.mark.parametrize("target", ["program", "directory", "asset", "config", "lock"])
def test_symlinks_cannot_escape(context, tmp_path, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "external.md"
    external.write_text("untouched")
    if target == "program":
        context.layout.program.unlink()
        context.layout.program.symlink_to(external)
        key = "agent.too"
    elif target == "directory":
        (context.home / "psyches").symlink_to(outside, target_is_directory=True)
        key = "psyches/new.md"
    elif target == "asset":
        parent = context.home / "skills/a/assets"
        parent.mkdir(parents=True)
        (parent / "external.md").symlink_to(external)
        key = "skills/a/assets/external.md"
    elif target == "config":
        (context.home / "config.toml").symlink_to(external)
        key = "config.toml"
    else:
        (context.home / ".caps.lock").symlink_to(external)
        key = "prompts/new.md"
    result = invoke(context, "create", key=key, content="changed")
    assert result.output["error"] == "io_error"
    assert external.read_text() == "untouched"
    assert not (outside / "new.md").exists()


def test_two_writers_cannot_overwrite_observed_snapshot(context):
    item = read(context, "agent.too")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                invoke,
                context,
                "update",
                key="agent.too",
                content=f"agic:\n  Version {index}.\n",
                if_digest=item["digest"],
            )
            for index in range(2)
        ]
        results = [future.result() for future in futures]
    assert sum(result.error is None for result in results) == 1
    assert (
        next(result for result in results if result.error).output["error"]
        == "digest_mismatch"
    )


def test_independent_files_can_be_saved_before_composition_is_valid(context):
    item = read(context, "agent.too")
    with ThreadPoolExecutor(max_workers=2) as pool:
        main = pool.submit(
            invoke,
            context,
            "update",
            key="agent.too",
            content="agic research:\n  Research.\n",
            if_digest=item["digest"],
        )
        flow = pool.submit(
            invoke,
            context,
            "create",
            key="flows/research.too",
            content="flow:\n  pass\n",
        )
        results = [main.result(), flow.result()]
    assert all(result.error is None for result in results)
    with pytest.raises(StatePreparationError):
        prepare_agent_state(context.layout)


@pytest.mark.parametrize("source_name", ["parrot.too", " parrot .too"])
def test_roaming_main_preserves_canonical_link_and_source_mode(tmp_path, source_name):
    from toolang.up.process import materialize_roaming_program

    source = tmp_path / source_name
    source.write_text("agic:\n  Original.\n")
    source.chmod(0o755)
    layout = materialize_roaming_program(source)
    context = MeToolContext(
        home=layout.home, room=layout.tool_room("me"), layout=layout
    )
    item = read(context, "agent.too")
    result = invoke(
        context,
        "update",
        key="agent.too",
        content="agic:\n  Changed.\n",
        if_digest=item["digest"],
    )
    assert result.error is None, result.output
    assert layout.program.is_symlink()
    assert sha256(source.read_bytes()).hexdigest() == result.output["digest"]
    assert source.stat().st_mode & 0o777 == 0o755
    assert (layout.home / ".config.toml.lock").exists()
    assert not (layout.home / ".project.lock").exists()


@pytest.mark.parametrize("exists", [False, True])
def test_valid_long_asset_filename_can_be_created_and_replaced(context, exists):
    key = "skills/review/assets/" + "a" * 251 + ".bin"
    path = context.home / key
    arguments = {"key": key, "content": "/wA=", "encoding": "base64"}
    operation = "create"
    if exists:
        path.parent.mkdir(parents=True)
        path.write_bytes(b"original")
        arguments["if_digest"] = read(context, key)["digest"]
        operation = "update"
    result = invoke(context, operation, **arguments)
    assert result.error is None, result.output
    assert path.read_bytes() == b"\xff\0"
    assert read(context, key)["digest"] == result.output["digest"]
    assert tuple(path.parent.iterdir()) == (path,)


@pytest.mark.parametrize("operation", ["list", "get", "update", "delete"])
def test_roaming_link_loop_returns_a_structured_error(tmp_path, operation):
    from toolang.up.process import materialize_roaming_program

    source = tmp_path / "parrot.too"
    source.write_text("agic:\n  Original.\n")
    layout = materialize_roaming_program(source)
    context = MeToolContext(
        home=layout.home, room=layout.tool_room("me"), layout=layout
    )
    layout.program.unlink()
    layout.program.symlink_to("agent.too")
    arguments = {} if operation == "list" else {"key": "agent.too"}
    if operation in {"update", "delete"}:
        arguments["if_digest"] = sha256(source.read_bytes()).hexdigest()
    if operation == "update":
        arguments["content"] = "replacement"
    result = invoke(context, operation, **arguments)
    assert result.output["error"] == "io_error"
    assert source.read_text() == "agic:\n  Original.\n"
    assert layout.program.is_symlink()


@pytest.mark.parametrize("relative", ["other.too", "other/parrot.too"])
def test_roaming_program_cannot_edit_another_home_source(tmp_path, relative):
    from toolang.up.process import materialize_roaming_program

    source = tmp_path / "parrot.too"
    source.write_text("agic:\n  Original.\n")
    layout = materialize_roaming_program(source)
    context = MeToolContext(
        home=layout.home, room=layout.tool_room("me"), layout=layout
    )
    other = tmp_path / relative
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("agic:\n  Untouched.\n")
    layout.program.unlink()
    layout.program.symlink_to(other)
    result = invoke(
        context,
        "update",
        key="agent.too",
        content="replacement",
        if_digest=sha256(other.read_bytes()).hexdigest(),
    )
    assert result.output["error"] == "io_error"
    assert other.read_text() == "agic:\n  Untouched.\n"


def test_loaded_receipts_follow_captured_state_not_disk_or_publication(
    context, monkeypatch
):
    from dataclasses import replace

    from toolang.execution.tools.me.storage import HomeFiles
    from toolang.state.prepare import load_agent_state

    before = prepare_agent_state(context.layout)
    original = read(context, "agent.too")
    updated = invoke(
        context,
        "update",
        key="agent.too",
        if_digest=original["digest"],
        content="agic alice():\n  Changed.\n",
    )
    created = invoke(
        context, "create", key="flows/research.too", content="flow:\n  pass\n"
    )
    receipts = [updated.output, created.output]
    assert updated.error is created.error is None
    after = prepare_agent_state(context.layout)
    old_context = replace(context, state=before)
    new_context = replace(context, state=after)
    assert invoke(old_context, "loaded", receipts=receipts).output == {
        "loaded": False,
        "revision": before.revision,
        "mismatches": [
            {"key": "agent.too", "digest": original["digest"]},
            {"key": "flows/research.too", "digest": None},
        ],
    }
    assert invoke(new_context, "loaded", receipts=receipts).output == {
        "loaded": True,
        "revision": after.revision,
        "mismatches": [],
    }
    deleted = invoke(
        context, "delete", key="flows/research.too", if_digest=created.output["digest"]
    )
    assert deleted.output == {"key": "flows/research.too", "digest": None}
    assert invoke(new_context, "loaded", receipts=[deleted.output]).output == {
        "loaded": False,
        "revision": after.revision,
        "mismatches": [created.output],
    }
    final = prepare_agent_state(context.layout)
    assert invoke(
        replace(context, state=final),
        "loaded",
        receipts=[updated.output, deleted.output],
    ).output == {
        "loaded": True,
        "revision": final.revision,
        "mismatches": [],
    }

    # Neither reloading an old revision nor comparing it consults current files.
    context.layout.program.write_text("agic (")
    restored = load_agent_state(context.layout, after.revision)
    assert restored.files == after.files

    def no_disk(*_args, **_kwargs):
        pytest.fail("loaded must not access home files")

    monkeypatch.setattr(HomeFiles, "read", no_disk)
    monkeypatch.setattr(HomeFiles, "lock", no_disk)
    monkeypatch.setattr(Path, "read_bytes", no_disk)
    assert (
        invoke(replace(context, state=restored), "loaded", receipts=receipts).output[
            "loaded"
        ]
        is True
    )


def test_loaded_uses_home_raw_config_and_keeps_shadowed_inputs(context):
    from dataclasses import replace

    from toolang.state.prepare import load_agent_state

    root_prompt = context.layout.root / "prompts/same.md"
    root_prompt.parent.mkdir()
    root_prompt.write_text("Root prompt.")
    (root_prompt.parent / "root-only.md").write_text("Only in root.")
    root_config = b'# Root comment\r\n[workspaces]\r\nignored = "."\r\n'
    context.layout.root_config.write_bytes(root_config)
    content = b'# Keep comment\r\n[workspaces]\r\nrepo = "."\r\n'
    config_receipt = invoke(
        context, "create", key="config.toml", content=content.decode()
    ).output
    prompt_receipt = invoke(
        context, "create", key="prompts/same.md", content="Home prompt."
    ).output
    context.layout.program.write_text(
        "prompt same:\n  Inline prompt.\n\nagic alice():\n  Hello.\n"
    )
    asset = invoke(
        context,
        "create",
        key="skills/review/assets/image.bin",
        content="/wA=",
        encoding="base64",
    ).output
    invoke(
        context,
        "create",
        key="skills/review/SKILL.md",
        content="---\ndescription: Review\n---\nReview.",
    )
    state = prepare_agent_state(context.layout)
    assert state.prompts["same"].scope == "here"
    assert invoke(
        replace(context, state=state),
        "loaded",
        receipts=[config_receipt, prompt_receipt, asset],
    ).output == {
        "loaded": True,
        "revision": state.revision,
        "mismatches": [],
    }
    assert state.workspaces["repo"] == str(context.home.resolve())
    files = state.to_snapshot()["files"]
    assert isinstance(files, list)
    assert {
        "scope": "root",
        "key": "config.toml",
        "digest": sha256(root_config).hexdigest(),
    } in files
    assert {
        "scope": "root",
        "key": "prompts/same.md",
        "digest": sha256(root_prompt.read_bytes()).hexdigest(),
    } in files
    assert {"scope": "home", **config_receipt} in files
    # A root-only definition must not satisfy a home receipt with the same key.
    assert invoke(
        replace(context, state=state),
        "loaded",
        receipts=[
            {
                "key": "prompts/root-only.md",
                "digest": sha256(b"Only in root.").hexdigest(),
            }
        ],
    ).output["mismatches"] == [{"key": "prompts/root-only.md", "digest": None}]
    assert load_agent_state(context.layout, state.revision).files == state.files


@pytest.mark.parametrize(
    "key",
    ["tasks/example.md", "chores/example.md", "prompts/missing.md", "untracked.txt"],
)
def test_loaded_untracked_or_absent_keys_use_null(context, key):
    from dataclasses import replace

    if key.startswith(("tasks/", "chores/")):
        assert (
            invoke(context, "create", key=key, content="Untracked job.").error is None
        )
    state = prepare_agent_state(context.layout)
    bound = replace(context, state=state)
    assert invoke(
        bound, "loaded", receipts=[{"key": key, "digest": "0" * 64}]
    ).output == {
        "loaded": False,
        "revision": state.revision,
        "mismatches": [{"key": key, "digest": None}],
    }
    for receipts in ([], [{"key": key, "digest": None}]):
        assert invoke(bound, "loaded", receipts=receipts).output == {
            "loaded": True,
            "revision": state.revision,
            "mismatches": [],
        }


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"receipts": None},
        {"receipts": {}},
        {"receipts": [None]},
        {"receipts": [{"key": "agent.too"}]},
        {"receipts": [{"key": "agent.too", "digest": "BAD"}]},
        {"receipts": [{"key": "../agent.too", "digest": None}]},
        {"receipts": [{"key": "agent.too", "digest": None, "scope": "root"}]},
        {"receipts": [{"key": "agent.too", "digest": None}] * 2},
        {"receipts": [], "revision": "0" * 64},
    ],
)
def test_loaded_rejects_invalid_receipts(context, arguments):
    result = invoke(context, "loaded", **arguments)
    assert result.error is not None
    assert result.output["error"] == "invalid_request"
    assert set(result.output) <= {"error", "message", "key"}


def test_loaded_requires_bound_state_without_preparing_one(context):
    result = invoke(context, "loaded", receipts=[])
    assert result.error is not None
    assert result.output["error"] == "invalid_request"
    assert not context.layout.agent_state.exists()


def test_flat_file_errors_and_digest_conflicts(context):
    original = read(context, "agent.too")
    stale = invoke(
        context, "update", key="agent.too", content="unused", if_digest="0" * 64
    )
    assert stale.error is not None
    assert stale.output == {
        "error": "digest_mismatch",
        "message": "file changed since it was read; get it again before editing",
        "key": "agent.too",
        "expected_digest": "0" * 64,
        "actual_digest": original["digest"],
    }
    for operation, arguments, code in (
        ("get", {"key": "flows/missing.too"}, "not_found"),
        (
            "update",
            {"key": "flows/missing.too", "content": "", "if_digest": "0" * 64},
            "not_found",
        ),
        ("delete", {"key": "flows/missing.too", "if_digest": "0" * 64}, "not_found"),
        ("create", {"key": "agent.too", "content": ""}, "already_exists"),
    ):
        result = invoke(context, operation, **arguments)
        assert result.error is not None
        assert set(result.output) == {"error", "message", "key"}
        assert result.output["error"] == code
        assert result.output["key"] == arguments["key"]
