"""Run-local workspace location is durable and drives path-aware tools."""

import asyncio

import tomlkit
import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.types import ThreadPrefix, ToolStepGiven
from toolang.plugin.toolsets.loading import load_tools


def test_cd_persists_location_and_relative_fs_uses_it(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    home = tmp_path / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        tomlkit.dumps({"workspaces": {"repo": str(repo)}})
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        tools=load_tools(queries=("fs/*", "shell/*")),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "chdir", "chdir", "_toolang__chdir", {"path": "repo://src"}
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "write",
                        "write",
                        "fs__write",
                        {"path": "result.txt", "text": "done"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            result = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            assert result.status == "succeeded", result.error
            assert (repo / "src/result.txt").read_text() == "done"
            assert harness.store.current_cwd(result.id) == "repo://src"
            assert [
                len(_locations(inv.call)) for inv in harness.adapter.invocations
            ] == [1, 2, 3]
            assert [
                _workspace_lists(inv.call)[-1] for inv in harness.adapter.invocations
            ] == ["tmp,repo", "tmp,repo", "tmp,repo"]
            assert (
                "without an OS sandbox"
                in harness.adapter.invocations[0].call.instructions
            )
            assert [
                [
                    part.text
                    for message in call.call.messages
                    for part in message.parts
                    if isinstance(part, TextPart)
                    and part.text.startswith("<toolang:workdir ")
                ][-1]
                for call in harness.adapter.invocations
            ] == [
                '<toolang:workdir path="repo://"/>',
                '<toolang:workdir path="repo://src"/>',
                '<toolang:workdir path="repo://src"/>',
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["remap", "remove", "unavailable"])
def test_reload_invalidates_committed_cwd_before_next_model_call(tmp_path, change):
    from tests.support.execution_harness import AsyncGate, ScriptedModelTurn
    from toolang.execution.records import CwdControlPayload
    from toolang.state.prepare import prepare_agent_state

    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other"
    if change == "remap":
        other.mkdir()
    home = tmp_path / "agents" / "alice"
    home.mkdir(parents=True)
    config = home / "config.toml"
    config.write_text(tomlkit.dumps({"workspaces": {"repo": str(repo)}}))
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        tools=load_tools(queries=("fs/*",)),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall("chdir", "chdir", "_toolang__chdir", {"path": "repo://"}),
                )
            ),
            ScriptedModelTurn(
                result=ModelCallResult(
                    tool_calls=(ToolCall("tmp", "tmp", "fs__stat", {"path": "tmp://"}),)
                ),
                gate=gate,
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            await gate.wait_until_entered()
            assert harness.store.current_cwd(handle.run_id) == "repo://"
            config.write_text(
                tomlkit.dumps(
                    {"workspaces": {} if change == "remove" else {"repo": str(other)}}
                )
            )
            updated = prepare_agent_state(harness.setup.layout)
            control = handle.reload(updated)
            async with asyncio.timeout(3):
                while True:
                    applied = harness.store.get_run_control(
                        run_id=handle.run_id, index=control.index
                    )
                    if applied is not None and applied.status == "applied":
                        break
                    await asyncio.sleep(0.01)
            assert harness.store.current_cwd(handle.run_id) == "tmp://"
            changes = [
                c
                for c in harness.store.list_run_controls(
                    run_id=handle.run_id, kind="cwd"
                )
                if isinstance(c.payload, CwdControlPayload)
                and c.payload.cause == "invalidated"
            ]
            assert len(changes) == 1
            invalidation = changes[0].payload
            assert isinstance(invalidation, CwdControlPayload)
            assert invalidation.state == control.ref
            gate.release()
            result = await handle
            assert result.status == "succeeded", result.error
            assert _locations(harness.adapter.invocations[-1].call)[-1] == (
                '<toolang:workdir path="tmp://"/>'
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("count", [0, 1, 2])
def test_initial_workdir_uses_last_available_workspace(tmp_path, count):
    roots = {}
    for i in range(count):
        root = tmp_path / f"repo{i}"
        root.mkdir()
        roots[f"repo{i}"] = str(root)
    home = tmp_path / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "config.toml").write_text(tomlkit.dumps({"workspaces": roots}))
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            assert run.status == "succeeded", run.error
            assert harness.store.current_cwd(run.id) == (
                f"repo{count - 1}://" if count else "tmp://"
            )
            assert _workspace_lists(harness.adapter.invocations[0].call) == [
                ",".join(("tmp", *(f"repo{i}" for i in range(count))))
            ]

    asyncio.run(scenario())


def test_tmp_workspace_is_implicit_and_configured_tmp_cannot_replace_it(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    configured_tmp = tmp_path / "configured-tmp"
    configured_tmp.mkdir()
    home = tmp_path / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        tomlkit.dumps({"workspaces": {"repo": str(repo), "tmp": str(configured_tmp)}})
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        tools=load_tools(queries=("fs/*",)),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "write",
                        "write",
                        "fs__write",
                        {"path": "tmp://marker", "text": "scratch"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            assert run.status == "succeeded", run.error
            assert (home / ".tmp/marker").read_text() == "scratch"
            assert not (configured_tmp / "marker").exists()
            assert harness.store.current_cwd(run.id) == "repo://"
            assert _workspace_lists(harness.adapter.invocations[0].call) == ["tmp,repo"]

    asyncio.run(scenario())


def test_root_run_workdir_continues_from_previous_root_final_value(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        tomlkit.dumps({"workspaces": {"repo": str(repo)}})
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall("cd", "cd", "_toolang__chdir", {"path": "tmp://"}),
                )
            ),
            ModelCallResult(message=Message.assistant("first")),
            ModelCallResult(message=Message.assistant("second")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            first = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="chat")
            )
            second = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="chat")
            )
            assert first.status == second.status == "succeeded"
            assert harness.store.current_cwd(first.id) == "tmp://"
            assert (
                harness.executor.initial_workdir(harness.setup, harness.state, thread)
                == "tmp://"
            )
            assert harness.store.current_cwd(second.id) == "tmp://"
            assert _locations(harness.adapter.invocations[0].call) == [
                '<toolang:workdir path="repo://"/>'
            ]
            assert _locations(harness.adapter.invocations[-1].call)[-1] == (
                '<toolang:workdir path="tmp://"/>'
            )

    asyncio.run(scenario())


def test_cd_mixed_batch_is_rejected_before_filesystem_mutation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        tomlkit.dumps({"workspaces": {"repo": str(repo)}})
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        tools=load_tools(queries=("fs/*",)),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall("chdir", "chdir", "_toolang__chdir", {"path": "repo://"}),
                    ToolCall(
                        "write",
                        "write",
                        "fs__write",
                        {"path": "repo://bad", "text": "bad"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            assert run.status == "succeeded", run.error
            assert not (repo / "bad").exists()
            assert harness.store.list_run_controls(run_id=run.id, kind="cwd") == ()

    asyncio.run(scenario())


def _configured_home(tmp_path, roots):
    home = tmp_path / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        tomlkit.dumps({"workspaces": {name: str(root) for name, root in roots.items()}})
    )
    return home


def _locations(call):
    return [
        part.text
        for message in call.messages
        for part in message.parts
        if isinstance(part, TextPart) and part.text.startswith("<toolang:workdir ")
    ]


def _workspace_lists(call):
    return [
        part.text.split('list="', 1)[1].split('"', 1)[0]
        for message in call.messages
        for part in message.parts
        if isinstance(part, TextPart) and part.text.startswith("<toolang:workspace ")
    ]


def test_unavailable_workspace_is_omitted_and_tmp_remains_usable(tmp_path):
    missing = tmp_path / "missing"
    _configured_home(tmp_path, {"offline": missing})
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        tools=load_tools(queries=("fs/*",)),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "write",
                        "write",
                        "fs__write",
                        {"path": "offline://file", "text": "x"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            result = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            assert result.status == "succeeded", result.error
            assert harness.store.current_cwd(result.id) == "tmp://"
            assert not missing.exists()
            from toolang.base.types.message import ToolResultPart

            replies = [
                p
                for step in harness.store.list_steps(run_id=result.id)
                if step.kind == "tool" and step.output is not None
                for p in (step.output.local.value,)
                if isinstance(p, ToolResultPart)
            ]
            assert "not available" in (replies[0].error or "")
            assert _locations(harness.adapter.invocations[0].call) == [
                '<toolang:workdir path="tmp://"/>'
            ]
            assert _workspace_lists(harness.adapter.invocations[0].call) == ["tmp"]

    asyncio.run(scenario())


def test_child_cd_does_not_change_parent_or_sibling_location(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _configured_home(tmp_path, {"repo": repo})
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic child() -> Text:
  context = none
  user: Child.
flow parent:
  run child
  run child
""",
        prepare_state=True,
        tools=load_tools(queries=("fs/*",)),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "chdir", "chdir", "_toolang__chdir", {"path": "repo://src"}
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("first")),
            ModelCallResult(message=Message.assistant("second")),
        ],
    )

    async def scenario():
        async with harness:
            result = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="flow:parent",
                    primary=(TextPart("start"),),
                )
            )
            assert result.status == "succeeded", result.error
            root, first, sibling = harness.store.list_run_tree(root_run_id=result.id)
            assert root.id == result.id
            assert [
                harness.store.current_cwd(run.id) for run in (root, first, sibling)
            ] == ["repo://", "repo://src", "repo://"]
            assert (
                _locations(harness.adapter.invocations[-1].call)[-1]
                == '<toolang:workdir path="repo://"/>'
            )

    asyncio.run(scenario())


def test_cd_and_shell_use_run_location_without_persisting_command_cd(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _configured_home(tmp_path, {"repo": repo})
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        tools=load_tools(queries=("shell/*",)),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "chdir", "chdir", "_toolang__chdir", {"path": "repo://src"}
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall("up", "up", "shell__execute", {"command": "cd .. && pwd"}),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall("again", "again", "shell__execute", {"command": "pwd"}),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            assert run.status == "succeeded", run.error
            assert harness.store.current_cwd(run.id) == "repo://src"
            from toolang.base.types.message import ToolResultPart

            outputs = [
                step.output.local.value
                for step in harness.store.list_steps(run_id=run.id)
                if step.kind == "tool"
                and step.output is not None
                and isinstance(step.output.local.value, ToolResultPart)
            ]
            assert [p.output.get("cwd") for p in outputs] == [
                "repo://src",
                "repo://src",
                "repo://src",
            ]
            assert outputs[1].output["stdout"].strip() == str(repo)
            assert outputs[2].output["stdout"].strip() == str(repo / "src")

    asyncio.run(scenario())


def test_retry_discards_cd_control_at_agic_restart_anchor(tmp_path):
    from toolang.execution.store import RunStore

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _configured_home(tmp_path, {"repo": repo})
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "chdir", "chdir", "_toolang__chdir", {"path": "repo://src"}
                    ),
                )
            ),
            RuntimeError("temporary failure"),
            ModelCallResult(message=Message.assistant("resumed")),
            ModelCallResult(message=Message.assistant("from start")),
            ModelCallResult(message=Message.assistant("rerun")),
        ],
    )

    async def scenario():
        async with harness:
            first = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            assert first.status == "failed", first.error
            assert harness.store.current_cwd(first.id) == "repo://src"
            reopened = RunStore(harness.store.db_path)
            try:
                assert reopened.current_cwd(first.id) == "repo://src"
            finally:
                reopened.close()
            recovered = await harness.executor.retry(
                first.id, setup=harness.setup, state=harness.state
            )
            assert recovered.status == "succeeded", recovered.error
            # An Agic cycle restarts from its first Model Step; cd is in the
            # discarded suffix and cannot be restored by reading old output.
            assert harness.store.current_cwd(first.id) == "repo://"
            assert harness.store.list_run_controls(run_id=first.id, kind="cwd") == ()
            anchor = harness.store.list_steps(run_id=first.id)[0].ref
            restarted = await harness.executor.retry(
                first.id, setup=harness.setup, state=harness.state, anchor=anchor
            )
            assert restarted.status == "succeeded", restarted.error
            assert harness.store.current_cwd(first.id) == "repo://"
            assert harness.store.list_run_controls(run_id=first.id, kind="cwd") == ()
            assert (
                _locations(harness.adapter.invocations[-1].call)[-1]
                == '<toolang:workdir path="repo://"/>'
            )
            fresh = await harness.executor.rerun(
                first.id, setup=harness.setup, state=harness.state
            )
            assert fresh.status == "succeeded", fresh.error
            assert fresh.id != first.id
            assert harness.store.current_cwd(fresh.id) == "repo://"
            # Rerun has a new initial cwd even when near history contains old declarations.
            assert _locations(harness.adapter.invocations[-1].call)[-1] == (
                '<toolang:workdir path="repo://"/>'
            )

    asyncio.run(scenario())


def test_cd_honors_rules_before_committing_a_new_location(tmp_path):
    from toolang.base.types.message import ToolResultPart

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/AGENTS.md").write_text("Scoped instructions.")
    _configured_home(tmp_path, {"repo": repo})
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "first", "first", "_toolang__chdir", {"path": "repo://src"}
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "retry", "retry", "_toolang__chdir", {"path": "repo://src"}
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            assert run.status == "succeeded", run.error
            assert harness.store.current_cwd(run.id) == "repo://src"
            assert len(harness.store.list_run_controls(run_id=run.id, kind="cwd")) == 1
            tool_names = [
                step.given.call.name
                for step in harness.store.list_steps(run_id=run.id)
                if isinstance(step.given, ToolStepGiven)
            ]
            assert tool_names == [
                "_toolang__honor",
                "_toolang__chdir",
            ]
            results = [
                part
                for message in harness.adapter.invocations[1].call.messages
                for part in message.parts
                if isinstance(part, ToolResultPart)
            ]
            assert len(results) == 1
            assert "Workspace rules were just loaded" in (results[0].error or "")
            assert [
                _locations(inv.call)[-1] for inv in harness.adapter.invocations
            ] == [
                '<toolang:workdir path="repo://"/>',
                '<toolang:workdir path="repo://"/>',
                '<toolang:workdir path="repo://src"/>',
            ]

    asyncio.run(scenario())


def test_failed_and_canceled_cd_never_change_the_run_location(tmp_path, monkeypatch):
    from tests.support.execution_harness import AsyncGate
    from toolang.execution.tools._toolang import ToolangTool

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _configured_home(tmp_path, {"repo": repo})
    gate = AsyncGate()
    original_invoke = ToolangTool.invoke

    async def invoke(self, arguments, context):
        if self.name == "chdir" and arguments.get("path") == "repo://src":
            await gate.wait()
        return await original_invoke(self, arguments, context)

    monkeypatch.setattr(ToolangTool, "invoke", invoke)
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "bad", "bad", "_toolang__chdir", {"path": "repo://missing"}
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall("slow", "slow", "_toolang__chdir", {"path": "repo://src"}),
                )
            ),
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=2)
            handle.cancel()
            gate.release()
            run = await asyncio.wait_for(handle, timeout=3)
            assert run.status == "canceled"
            assert harness.store.current_cwd(run.id) == "repo://"
            assert harness.store.list_run_controls(run_id=run.id, kind="cwd") == ()

    asyncio.run(scenario())


def test_explicit_path_without_cwd_and_unicode_cwd_declaration(tmp_path):
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    (repo / "a b%中").mkdir(parents=True)
    other.mkdir()
    _configured_home(tmp_path, {"repo": repo, "other": other})
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        tools=load_tools(queries=("fs/*",)),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "write",
                        "write",
                        "fs__write",
                        {"path": "repo://a%20b%25%E4%B8%AD/data", "text": "ok"},
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "cd",
                        "cd",
                        "_toolang__chdir",
                        {"path": "repo://a%20b%25%E4%B8%AD"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            assert run.status == "succeeded", run.error
            assert (repo / "a b%中/data").read_text() == "ok"
            assert harness.store.current_cwd(run.id) == "repo://a%20b%25%E4%B8%AD"
            assert [
                _locations(inv.call)[-1] for inv in harness.adapter.invocations
            ] == [
                '<toolang:workdir path="other://"/>',
                '<toolang:workdir path="other://"/>',
                '<toolang:workdir path="repo://a%20b%25%E4%B8%AD"/>',
            ]

    asyncio.run(scenario())


def test_guest_tool_paths_use_captured_mount_not_state_host_source(tmp_path):
    from dataclasses import replace
    from toolang.base.types.message import ToolResultPart

    host = tmp_path / "host-root"
    guest = tmp_path / "guest-root"
    host.mkdir()
    guest.mkdir()
    home = _configured_home(tmp_path, {"repo": host})
    tmp_root = home / ".tmp"
    tmp_root.mkdir()
    guest_tmp = guest / "tmp-workspace"
    guest_tmp.mkdir()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        tools=load_tools(queries=("fs/*",)),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "write", "write", "fs__write", {"path": "file", "text": "guest"}
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    assert harness.setup.environment is not None
    harness.setup = replace(
        harness.setup,
        environment=replace(
            harness.setup.environment,
            sandbox="docker:test",
            container=True,
            workspace_mounts={
                "tmp": (tmp_root, guest_tmp),
                "repo": (host, guest),
            },
            workspace_location="guest",
        ),
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            assert run.status == "succeeded", run.error
            assert harness.store.current_cwd(run.id) == "repo://"
            assert (guest / "file").read_text() == "guest"
            assert not (host / "file").exists()
            written = [
                step.output.local.value
                for step in harness.store.list_steps(run_id=run.id)
                if step.kind == "tool"
                and step.output is not None
                and isinstance(step.output.local.value, ToolResultPart)
            ]
            assert written[0].output["path"] == "repo://file"

    asyncio.run(scenario())


def test_reload_invalidates_active_child_cwd_without_retargeting_its_parent(tmp_path):
    from tests.support.execution_harness import AsyncGate, ScriptedModelTurn
    from toolang.execution.records import CwdControlPayload
    from toolang.state.prepare import prepare_agent_state

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    home = _configured_home(tmp_path, {"repo": repo})
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic child() -> Text:
  context = none
  user: Child.
flow parent:
  run child
""",
        prepare_state=True,
        tools=load_tools(queries=("fs/*",)),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "chdir", "chdir", "_toolang__chdir", {"path": "repo://src"}
                    ),
                )
            ),
            ScriptedModelTurn(
                result=ModelCallResult(
                    tool_calls=(ToolCall("tmp", "tmp", "fs__stat", {"path": "tmp://"}),)
                ),
                gate=gate,
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="flow:parent",
                    primary=(TextPart("seed"),),
                )
            )
            await gate.wait_until_entered()
            parent, child = harness.store.list_run_tree(root_run_id=handle.run_id)
            assert harness.store.current_cwd(parent.id) == "repo://"
            assert harness.store.current_cwd(child.id) == "repo://src"
            (home / "config.toml").write_text(
                tomlkit.dumps({"workspaces": {"repo": str(replacement)}})
            )
            control = handle.reload(prepare_agent_state(harness.setup.layout))
            async with asyncio.timeout(3):
                while True:
                    applied = harness.store.get_run_control(
                        run_id=parent.id, index=control.index
                    )
                    if applied is not None and applied.status == "applied":
                        break
                    await asyncio.sleep(0.01)
            assert [harness.store.current_cwd(run.id) for run in (parent, child)] == [
                "tmp://",
                "tmp://",
            ]
            for run in (parent, child):
                invalidations = [
                    c.payload
                    for c in harness.store.list_run_controls(run_id=run.id, kind="cwd")
                    if isinstance(c.payload, CwdControlPayload)
                    and c.payload.cause == "invalidated"
                ]
                assert len(invalidations) == 1
                assert invalidations[0].state == control.ref
            gate.release()
            result = await handle
            assert result.status == "succeeded", result.error
            assert (
                _locations(harness.adapter.invocations[-1].call)[-1]
                == '<toolang:workdir path="tmp://"/>'
            )

    asyncio.run(scenario())


def test_retry_rejects_applied_reload_and_rerun_uses_new_root(tmp_path):
    from tests.support.execution_harness import AsyncGate, ScriptedModelTurn
    from toolang.state.prepare import prepare_agent_state

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    home = _configured_home(tmp_path, {"repo": repo})
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        tools=load_tools(queries=("fs/*",)),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "chdir", "chdir", "_toolang__chdir", {"path": "repo://src"}
                    ),
                )
            ),
            ScriptedModelTurn(
                result=ModelCallResult(
                    tool_calls=(ToolCall("tmp", "tmp", "fs__stat", {"path": "tmp://"}),)
                ),
                gate=gate,
            ),
            ModelCallResult(message=Message.assistant("done")),
            ModelCallResult(message=Message.assistant("retried")),
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            await gate.wait_until_entered()
            (home / "config.toml").write_text(
                tomlkit.dumps({"workspaces": {"repo": str(other)}})
            )
            updated = prepare_agent_state(harness.setup.layout)
            control = handle.reload(updated)
            async with asyncio.timeout(3):
                while True:
                    applied = harness.store.get_run_control(
                        run_id=handle.run_id, index=control.index
                    )
                    if applied is not None and applied.status == "applied":
                        break
                    await asyncio.sleep(0.01)
            gate.release()
            completed = await handle
            assert completed.status == "succeeded", completed.error
            assert harness.store.current_cwd(completed.id) == "tmp://"
            first_step = harness.store.list_steps(run_id=completed.id)[0].ref
            with pytest.raises(ValueError, match="applied Agent State reloads"):
                harness.executor.retry(
                    completed.id,
                    setup=harness.setup,
                    state=harness.state,
                    anchor=first_step,
                )
            fresh = await harness.executor.rerun(
                completed.id, setup=harness.setup, state=updated
            )
            assert fresh.status == "succeeded", fresh.error
            assert fresh.id != completed.id
            assert harness.store.current_cwd(completed.id) == "tmp://"
            assert harness.store.current_cwd(fresh.id) == "repo://"

    asyncio.run(scenario())


def test_same_run_execute_retains_committed_cwd(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _configured_home(tmp_path, {"repo": repo})
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller() -> Text:
  context = none
  handoffs = agic:target
  user: Begin.
agic target() -> Text:
  context = none
  user: Continue.
""",
        prepare_state=True,
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "chdir", "chdir", "_toolang__chdir", {"path": "repo://src"}
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "execute",
                        "execute",
                        "_toolang__execute",
                        {"runnable": "target", "input": {}},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="agic:caller",
                )
            )
            assert run.status == "succeeded", run.error
            assert harness.store.current_cwd(run.id) == "repo://src"
            assert (
                _locations(harness.adapter.invocations[-1].call)[-1]
                == '<toolang:workdir path="repo://src"/>'
            )
            assert len(harness.store.list_run_tree(root_run_id=run.id)) == 1

    asyncio.run(scenario())
