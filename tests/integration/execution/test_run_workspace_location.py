"""Run-local workspace location is durable and drives path-aware tools."""

import asyncio

import tomlkit
import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.types import ThreadPrefix
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
                    ToolCall("cd", "cd", "_toolang__cd", {"path": ":repo://src"}),
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
            assert harness.store.current_cwd(result.id) == ":repo://src"
            assert [
                [
                    part.text
                    for message in call.call.messages
                    for part in message.parts
                    if isinstance(part, TextPart)
                    and part.text.startswith("<toolang:working-location ")
                ][-1]
                for call in harness.adapter.invocations
            ] == [
                '<toolang:working-location workspace="repo" workdir=""/>',
                '<toolang:working-location workspace="repo" workdir="src"/>',
                '<toolang:working-location workspace="repo" workdir="src"/>',
            ]

    asyncio.run(scenario())


def test_reload_remap_invalidates_committed_cwd_before_next_model_call(tmp_path):
    from tests.support.execution_harness import AsyncGate, ScriptedModelTurn
    from toolang.execution.records import CwdControlPayload
    from toolang.state.prepare import prepare_agent_state

    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other"
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
        responses=[
            ModelCallResult(
                tool_calls=(ToolCall("cd", "cd", "_toolang__cd", {"path": ":repo://"}),)
            ),
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("done")), gate=gate
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
            await gate.wait_until_entered()
            assert harness.store.current_cwd(handle.run_id) == ":repo://"
            config.write_text(tomlkit.dumps({"workspaces": {"repo": str(other)}}))
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
            assert harness.store.current_cwd(handle.run_id) == ""
            changes = [
                c
                for c in harness.store.list_run_controls(
                    run_id=handle.run_id, kind="cwd"
                )
                if isinstance(c.payload, CwdControlPayload)
                and c.payload.cause == "invalidated"
            ]
            assert len(changes) == 1
            change = changes[0].payload
            assert isinstance(change, CwdControlPayload)
            assert change.state == control.ref
            gate.release()
            result = await handle
            assert result.status == "succeeded", result.error

    asyncio.run(scenario())


@pytest.mark.parametrize("count", [0, 1, 2])
def test_initial_workspace_is_selected_only_when_unambiguous_and_available(
    tmp_path, count
):
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
                ":repo0://" if count == 1 else ""
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
                    ToolCall("cd", "cd", "_toolang__cd", {"path": ":repo://"}),
                    ToolCall(
                        "write",
                        "write",
                        "fs__write",
                        {"path": ":repo://bad", "text": "bad"},
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
