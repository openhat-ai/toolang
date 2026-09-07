"""Cwd grants survive execution without becoming State or ambient authority."""

import asyncio
from dataclasses import replace

import pytest

from tests.integration.execution.test_honor_rules import (
    SOURCE,
    _harness,
    _spec,
    _call,
    _calls,
    _answer,
    _recalls,
    _tool_steps,
    _workspace_state,
)
from tests.support.execution_harness import (
    AsyncGate,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from tests.support.execution_assertions import (
    assert_replayed,
    assert_run_event_integrity,
)
from toolang.base.types.message import message_text
from toolang.base.types.run import ToolCall
from toolang.base.utils.workspace_paths import capture_cwd
from toolang.execution.executor import RunExecutor
from toolang.execution.records import (
    RunControlPayload,
    control_payload_from_data,
    control_payload_to_data,
)
from toolang.execution.schemas import RetryRequest, RerunRequest
from toolang.execution.store import RunStore
from toolang.execution.types import RulesRecallTarget
from toolang.state.watcher import StateRefresh


@pytest.mark.parametrize("registered", [False, True])
def test_cwd_rules_identity_persistence_and_replay(tmp_path, registered):
    harness, repo, state = _harness(
        tmp_path,
        [
            _calls(_call("first", path="workspace://./result", text="ok")),
            _calls(_call("retry", path="workspace://./result", text="ok")),
            _answer(),
        ],
        source=SOURCE.replace("  context: none\n", ""),
    )
    if registered:
        directory = repo / "src"
        expected = [RulesRecallTarget("repo", "/"), RulesRecallTarget("repo", "/src")]
        uri = "workspace://repo/src"
    else:
        directory = tmp_path / "cwd"
        directory.mkdir()
        (directory / "AGENTS.md").write_text("Temporary workspace rules.")
        expected = [RulesRecallTarget(".", "/")]
        uri = "workspace://."
    cwd = capture_cwd(directory, state.workspaces)
    tracer = RecordingRunTracer()
    database = harness.store.db_path

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                replace(_spec(harness, state), cwd=cwd), tracer=tracer
            )
            assert run.status == "succeeded", run.error
            assert (directory / "result").read_text() == "ok"
            assert [c.payload.target for c in _recalls(harness, run)] == expected
            assert (
                _tool_steps(harness, run)[-1].output.value.output["path"]
                == uri + "/result"
            )
            payload = harness.store.get_run_control(run_id=run.id, index=0).payload
            assert payload.cwd == cwd
            encoded = control_payload_to_data(payload)
            assert encoded["cwd"] == {
                "resolved": str(directory.resolve()),
                "workspace": cwd.workspace,
                "relative": cwd.relative,
            }
            assert control_payload_from_data("run", encoded) == payload
            legacy = control_payload_from_data(
                "run", {k: v for k, v in encoded.items() if k != "cwd"}
            )
            assert isinstance(legacy, RunControlPayload) and legacy.cwd is None
            assert any(
                "cwd: workspace://./" in message_text(m.parts)
                for m in harness.adapter.invocations[0].call.messages
            )
            assert "workspace://." in harness.adapter.invocations[0].call.instructions
            assert_run_event_integrity(tracer.events)
            return run.id

    run_id = asyncio.run(scenario())
    restored = RunStore(database)
    try:
        control = restored.get_run_control(run_id=run_id, index=0)
        assert control is not None and isinstance(control.payload, RunControlPayload)
        assert control.payload.cwd == cwd
    finally:
        restored.close()
    (directory / "AGENTS.md").unlink()
    assert_replayed(database, tracer.events)


@pytest.mark.parametrize("change", ["remove", "remap"])
def test_reload_cannot_convert_a_named_cwd_to_temporary(tmp_path, change):
    changed = None

    async def refresh():
        assert changed is not None
        return StateRefresh(changed)

    harness, repo, state = _harness(
        tmp_path,
        [
            _calls(ToolCall("reload", "reload", "_toolang__reload", {})),
            _calls(_call("write", path="workspace://./result", text="bad")),
            _answer(),
        ],
        refresh_state=refresh,
        source=SOURCE.replace("  context: none\n", ""),
    )
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    changed = _workspace_state(
        harness, {} if change == "remove" else {"repo": replacement}
    )
    cwd = capture_cwd(repo, state.workspaces)

    async def scenario():
        async with harness:
            run = await harness.executor.run(replace(_spec(harness, state), cwd=cwd))
            assert run.status == "succeeded", run.error
            result = _tool_steps(harness, run)[-1].output.value
            assert result.error is not None
            assert not (repo / "result").exists()
            assert not (replacement / "result").exists()
            assert not _recalls(harness, run)
            for invocation in harness.adapter.invocations:
                context = "\n".join(
                    message_text(m.parts) for m in invocation.call.messages
                )
                assert "cwd: workspace://./" in context
                assert "cwd: workspace://repo" not in context

    asyncio.run(scenario())


def test_parallel_roots_keep_independent_cwd_grants_and_shell_does_not_inherit(
    tmp_path,
):
    gate = AsyncGate()
    harness, _repo, state = _harness(
        tmp_path,
        [
            ScriptedModelTurn(
                _calls(_call("a", path="workspace://./result", text="a")), gate=gate
            ),
            _calls(_call("b", path="workspace://./result", text="b")),
            _calls(
                _call(
                    "shell",
                    "shell__execute",
                    workspace=".",
                    cwd="/",
                    command="printf bad > shell-result",
                )
            ),
            _answer(),
            _answer(),
        ],
    )
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()

    async def scenario():
        async with harness:
            first = harness.executor.run(
                replace(_spec(harness, state), cwd=capture_cwd(a, state.workspaces))
            )
            await gate.wait_until_entered()
            second = await harness.executor.run(
                replace(_spec(harness, state), cwd=capture_cwd(b, state.workspaces))
            )
            assert second.status == "succeeded", second.error
            assert (b / "result").read_text() == "b"
            assert not (a / "result").exists()
            assert _tool_steps(harness, second)[-1].output.value.error is not None
            assert not (b / "shell-result").exists()
            gate.release()
            assert (await first).status == "succeeded"
            assert (a / "result").read_text() == "a"
            assert (b / "result").read_text() == "b"

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["run", "execute", "flow"])
def test_children_and_execute_inherit_cwd_without_copying_grants(tmp_path, operation):
    directive = "hands" if operation == "run" else "handoffs"
    source = f"""
agic chat() -> Text:
  {directive} = agic:child
  context: none
  user: Call the child.

agic child() -> Text:
  context: none
  user: Write the file.
"""
    responses = [
        _calls(
            ToolCall(
                "child", "child", f"_toolang__{operation}", {"runnable": "agic:child"}
            )
        ),
        _calls(_call("write", path="workspace://./result", text="child")),
        _answer(),
    ]
    if operation == "run":
        responses.append(_answer())
    elif operation == "flow":
        source = source[source.index("agic child") :]
        source += "\nflow chat() -> Text:\n  run child\n"
        responses = responses[1:]
    harness, _repo, state = _harness(tmp_path, responses, source=source)
    directory = tmp_path / "cwd"
    directory.mkdir()
    cwd = capture_cwd(directory, state.workspaces)

    async def scenario():
        async with harness:
            spec = _spec(harness, state)
            if operation == "flow":
                spec = replace(
                    spec, bindings=replace(spec.bindings, runnable="flow:chat")
                )
            run = await harness.executor.run(replace(spec, cwd=cwd))
            assert run.status == "succeeded", run.error
            assert (directory / "result").read_text() == "child"
            children = [r for r in harness.store.list_runs() if r.parent is not None]
            assert len(children) == (0 if operation == "execute" else 1)
            for child in children:
                assert (
                    harness.store.get_run_control(run_id=child.id, index=0).payload.cwd
                    is None
                )

    asyncio.run(scenario())


def test_temporary_cwd_retry_needs_renewed_authorization_after_restart(tmp_path):
    harness, _repo, state = _harness(
        tmp_path,
        [
            RuntimeError("temporary failure"),
            _calls(_call("write", path="workspace://./result", text="retried")),
            _answer(),
            _calls(_call("rerun", path="workspace://./second", text="reran")),
            _answer(),
        ],
    )
    directory = tmp_path / "cwd"
    directory.mkdir()
    cwd = capture_cwd(directory, state.workspaces)

    async def scenario():
        run = await harness.executor.run(replace(_spec(harness, state), cwd=cwd))
        assert run.status == "failed"
        await harness.executor.stop()
        harness.store.close()
        store = RunStore(harness.setup.layout.run_store)
        executor = RunExecutor(
            store,
            harness.ids,
            setup=lambda: harness.setup,
            state=lambda: state,
            load_state=lambda _revision: state,
        )
        try:
            before = store.list_steps(run_id=run.id)
            for unauthorized in (None, capture_cwd(tmp_path, state.workspaces)):
                with pytest.raises(ValueError, match="renewed authorization"):
                    executor.retry(RetryRequest(run.id, (), "denied", cwd=unauthorized))
                assert store.list_steps(run_id=run.id) == before
            retried = await executor.retry(RetryRequest(run.id, (), "retry", cwd=cwd))
            assert retried.status == "succeeded", retried.error
            assert (directory / "result").read_text() == "retried"
            another = tmp_path / "another"
            another.mkdir()
            new_cwd = capture_cwd(another, state.workspaces)
            rerun = await executor.rerun(RerunRequest(run.id, (), "rerun", cwd=new_cwd))
            assert rerun.status == "succeeded", rerun.error
            assert (another / "second").read_text() == "reran"
            assert not (directory / "second").exists()
        finally:
            await executor.stop()
            store.close()

    asyncio.run(scenario())
