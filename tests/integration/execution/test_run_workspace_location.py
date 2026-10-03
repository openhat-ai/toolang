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
            ] == ["lab,repo", "lab,repo", "lab,repo"]
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
def test_publication_preserves_committed_cwd_before_next_model_call(tmp_path, change):
    from tests.support.execution_harness import AsyncGate, ScriptedModelTurn
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
                    tool_calls=(ToolCall("lab", "lab", "fs__stat", {"path": "lab://"}),)
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
            harness.published = updated
            assert harness.store.current_cwd(handle.run_id) == "repo://"
            assert (
                len(harness.store.list_run_controls(run_id=handle.run_id, kind="cwd"))
                == 1
            )
            gate.release()
            result = await handle
            assert result.status == "succeeded", result.error
            assert _locations(harness.adapter.invocations[-1].call)[-1] == (
                '<toolang:workdir path="repo://"/>'
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
                f"repo{count - 1}://" if count else "lab://"
            )
            assert _workspace_lists(harness.adapter.invocations[0].call) == [
                ",".join(("lab", *(f"repo{i}" for i in range(count))))
            ]

    asyncio.run(scenario())


def test_lab_workspace_is_implicit_and_old_scratch_is_untouched(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    configured_lab = tmp_path / "configured-lab"
    configured_lab.mkdir()
    home = tmp_path / "agents" / "alice"
    home.mkdir(parents=True)
    old_scratch = home / ".tmp"
    old_scratch.mkdir()
    (old_scratch / "existing.txt").write_text("leave me alone")
    (home / "config.toml").write_text(
        tomlkit.dumps({"workspaces": {"repo": str(repo), "lab": str(configured_lab)}})
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
                        {"path": "lab://marker", "text": "scratch"},
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
            assert (home / "lab/marker").read_text() == "scratch"
            assert not (home / "lab/existing.txt").exists()
            assert not (configured_lab / "marker").exists()
            assert (old_scratch / "existing.txt").read_text() == "leave me alone"
            assert not (old_scratch / "marker").exists()
            assert harness.store.current_cwd(run.id) == "repo://"
            assert _workspace_lists(harness.adapter.invocations[0].call) == ["lab,repo"]

    asyncio.run(scenario())


def test_explicit_workspace_named_tmp_remains_configurable(tmp_path):
    configured = tmp_path / "configured-tmp"
    configured.mkdir()
    home = tmp_path / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        tomlkit.dumps({"workspaces": {"tmp": str(configured)}})
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
                        "write-tmp",
                        "write-tmp",
                        "fs__write",
                        {"path": "tmp://marker", "text": "configured"},
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
            assert (configured / "marker").read_text() == "configured"
            assert (home / "lab").is_dir()
            assert _workspace_lists(harness.adapter.invocations[0].call) == ["lab,tmp"]
            assert harness.store.current_cwd(run.id) == "tmp://"

    asyncio.run(scenario())


def test_saved_former_implicit_workdir_falls_back_to_lab(tmp_path):
    from toolang.state.prepare import prepare_agent_state

    configured = tmp_path / "configured-tmp"
    configured.mkdir()
    home = tmp_path / "agents" / "alice"
    home.mkdir(parents=True)
    config = home / "config.toml"
    config.write_text(tomlkit.dumps({"workspaces": {"tmp": str(configured)}}))
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="chat")
            )
            assert run.status == "succeeded", run.error
            assert harness.store.current_cwd(run.id) == "tmp://"
            config.write_text(tomlkit.dumps({"workspaces": {}}))
            updated = prepare_agent_state(harness.setup.layout)
            assert (
                harness.executor.initial_workdir(harness.setup, updated, thread)
                == "lab://"
            )

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
                    ToolCall("cd", "cd", "_toolang__chdir", {"path": "lab://"}),
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
            assert harness.store.current_cwd(first.id) == "lab://"
            assert (
                harness.executor.initial_workdir(harness.setup, harness.state, thread)
                == "lab://"
            )
            assert harness.store.current_cwd(second.id) == "lab://"
            assert _locations(harness.adapter.invocations[0].call) == [
                '<toolang:workdir path="repo://"/>'
            ]
            assert _locations(harness.adapter.invocations[-1].call)[-1] == (
                '<toolang:workdir path="lab://"/>'
            )

    asyncio.run(scenario())


def test_new_session_inherits_latest_finished_root_workdir_across_threads(tmp_path):
    from tests.support.execution_harness import AsyncGate, ScriptedModelTurn

    repo = tmp_path / "repo"
    for name in ("first", "second", "active"):
        (repo / name).mkdir(parents=True)
    _configured_home(tmp_path, {"repo": repo})
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  context = none\n  user: Start.\n",
        prepare_state=True,
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "first", "first", "_toolang__chdir", {"path": "repo://first"}
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("first done")),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "second", "second", "_toolang__chdir", {"path": "repo://second"}
                    ),
                )
            ),
            RuntimeError("terminal model failure"),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "active", "active", "_toolang__chdir", {"path": "repo://active"}
                    ),
                )
            ),
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("active done")),
                gate=gate,
            ),
        ],
    )

    async def scenario():
        async with harness:
            first_thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            first = await harness.executor.run(
                harness.run_spec(thread=first_thread, runnable="chat")
            )
            second_thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            second = await harness.executor.run(
                harness.run_spec(thread=second_thread, runnable="chat")
            )
            active_thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            active = harness.executor.run(
                harness.run_spec(thread=active_thread, runnable="chat")
            )
            await gate.wait_until_entered()
            assert first.status == "succeeded"
            assert second.status == "failed"
            assert harness.store.current_cwd(active.run_id) == "repo://active"
            assert (
                harness.executor.initial_workdir(harness.setup, harness.state)
                == "repo://second"
            )
            assert (
                harness.executor.initial_workdir(
                    harness.setup, harness.state, first_thread
                )
                == "repo://first"
            )
            gate.release()
            completed = await active
            assert completed.status == "succeeded", completed.error

    asyncio.run(scenario())


def test_new_session_ignores_child_run_workdir(tmp_path):
    repo = tmp_path / "repo"
    for name in ("parent", "child"):
        (repo / name).mkdir(parents=True)
    _configured_home(tmp_path, {"repo": repo})
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text) -> Text:
  hands = agic:child
  context = none
  user: Start {{_}}.

agic child(_: Text) -> Text:
  context = none
  user: Start {{_}}.
""",
        prepare_state=True,
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "parent", "parent", "_toolang__chdir", {"path": "repo://parent"}
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "run-child",
                        "run-child",
                        "_toolang__run",
                        {
                            "runnable": "agic:child",
                            "input": {"_": "child"},
                        },
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "child", "child", "_toolang__chdir", {"path": "repo://child"}
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("child done")),
            ModelCallResult(message=Message.assistant("parent done")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=(TextPart("start"),),
                )
            )
            children = [
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.parent is not None
            ]
            assert root.status == "succeeded", root.error
            assert len(children) == 1
            assert harness.store.current_cwd(root.id) == "repo://parent"
            assert harness.store.current_cwd(children[0].id) == "repo://child"
            assert (
                harness.executor.initial_workdir(harness.setup, harness.state)
                == "repo://parent"
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


def test_unavailable_workspace_is_omitted_and_lab_remains_usable(tmp_path):
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
            assert harness.store.current_cwd(result.id) == "lab://"
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
                '<toolang:workdir path="lab://"/>'
            ]
            assert _workspace_lists(harness.adapter.invocations[0].call) == ["lab"]

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


def test_chdir_loads_rules_only_when_a_path_operation_follows(tmp_path):
    from toolang.base.types.message import ToolResultPart

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/AGENTS.md").write_text("Scoped instructions.")
    _configured_home(tmp_path, {"repo": repo})
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
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "write",
                        "write",
                        "fs__write",
                        {"path": "repo://src/result", "text": "done"},
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "retry",
                        "retry",
                        "fs__write",
                        {"path": "repo://src/result", "text": "done"},
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
            assert (repo / "src/result").read_text() == "done"
            assert harness.store.current_cwd(run.id) == "repo://src"
            tool_names = [
                step.given.call.name
                for step in harness.store.list_steps(run_id=run.id)
                if isinstance(step.given, ToolStepGiven)
            ]
            assert tool_names == ["_toolang__chdir", "_toolang__honor", "fs__write"]
            results = [
                part
                for message in harness.adapter.invocations[2].call.messages
                for part in message.parts
                if isinstance(part, ToolResultPart)
                and "Workspace rules were just loaded" in (part.error or "")
            ]
            assert len(results) == 1
            assert [
                _locations(inv.call)[-1] for inv in harness.adapter.invocations
            ] == [
                '<toolang:workdir path="repo://"/>',
                '<toolang:workdir path="repo://src"/>',
                '<toolang:workdir path="repo://src"/>',
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
    lab_root = home / "lab"
    lab_root.mkdir()
    guest_lab = guest / "lab-workspace"
    guest_lab.mkdir()
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
                "lab": (lab_root, guest_lab),
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


def test_publication_preserves_active_child_and_parent_cwd(tmp_path):
    from tests.support.execution_harness import AsyncGate, ScriptedModelTurn
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
                    tool_calls=(ToolCall("lab", "lab", "fs__stat", {"path": "lab://"}),)
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
            harness.published = prepare_agent_state(harness.setup.layout)
            assert [harness.store.current_cwd(run.id) for run in (parent, child)] == [
                "repo://",
                "repo://src",
            ]
            assert not harness.store.list_run_controls(run_id=parent.id, kind="cwd")
            assert (
                len(harness.store.list_run_controls(run_id=child.id, kind="cwd")) == 1
            )
            gate.release()
            result = await handle
            assert result.status == "succeeded", result.error
            assert (
                _locations(harness.adapter.invocations[-1].call)[-1]
                == '<toolang:workdir path="repo://src"/>'
            )

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
                        "_toolang__exec",
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
