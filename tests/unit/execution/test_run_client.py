"""Run client boundary and process-local adapter behavior."""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from pathlib import Path
from typing import cast

import pytest

from tests.support.execution_assertions import without_runtime_snapshots
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
    TEST_MODEL_REF,
)
from toolang.base.errors import ToolangError
from toolang.base.types.message import Message, TextPart
from toolang.base.types.model import ModelOverride, ModelRequest
from toolang.base.types.policy import RunBindings, RunLimits, RunPolicy
from toolang.base.types.run import ModelCallResult
from toolang.execution.client import LocalRunClient, RunClient, RunHandle
from toolang.execution.executor import LocalRunHandle, RunExecutor
from toolang.execution.policy import materialize_policy
from toolang.execution.records import SteerControlPayload, CancelControlPayload
from toolang.execution.schemas import (
    RerunRequest,
    RunRequest,
    RunnableRequest,
)
from toolang.execution.types import ErrorMessage, RunCommand, ThreadKind
from toolang.lang.types import Array
from toolang.lang.input import CallInput
from toolang.setup import AgentSetup


# Keep publication-read assertions focused on Run acceptance, not model catalogs.
_CHAT_SOURCE = """
agic chat(_: Part[]) -> Part[]:
  hands = none
  handoffs = none
  recall = none
  context = none
  instruct = none
  user: {{_}}

agic session(_: Part[]) -> Part[]:
  hands = none
  handoffs = none
  recall = none
  context = none
  instruct = none
  user: {{_}}

agic selected(_: Part[]) -> Part[]:
  hands = none
  handoffs = none
  recall = none
  context = none
  instruct = none
  user: {{_}}
"""


def _request(
    thread: str,
    *,
    commands: tuple[RunCommand, ...] = (),
    session_commands: tuple[RunCommand, ...] = (),
    input: CallInput[str] = CallInput({"_": "hello"}),
    runnable_fallbacks: tuple[str, ...] = ("missing", "chat", "default"),
    request_id: str = "request_1",
) -> RunRequest:
    runnable = next(
        (
            candidate
            for candidate in runnable_fallbacks
            if candidate
            in {
                "chat",
                "agic:chat",
                "session",
                "agic:session",
                "selected",
                "agic:selected",
            }
        ),
        runnable_fallbacks[0] if runnable_fallbacks else "agic:chat",
    )
    if ":" not in runnable:
        runnable = f"agic:{runnable}"
    ceilings, bindings, limits = materialize_policy(
        RunBindings(model=TEST_MODEL_REF, runnable=runnable),
        RunLimits(),
        session=session_commands,
        run=commands,
    )
    return RunRequest(
        thread_id=thread,
        request_id=request_id,
        runnable=RunnableRequest(bindings.runnable or runnable, input),
        model=ModelRequest(bindings.model or TEST_MODEL_REF),
        policy=RunPolicy(allow=ceilings, limits=limits),
    )


def test_run_request_contains_only_materialized_caller_values() -> None:
    request = _request("chat_test")

    assert request == RunRequest(
        thread_id="chat_test",
        request_id="request_1",
        runnable=RunnableRequest("agic:chat", CallInput({"_": "hello"})),
        model=ModelRequest(TEST_MODEL_REF),
        policy=RunPolicy(),
    )
    assert {item.name for item in fields(RunRequest)} == {
        "thread_id",
        "request_id",
        "client_id",
        "runnable",
        "model",
        "policy",
        "workdir",
        "workdir_base",
        "attachments",
        "source_revision",
        "driver",
    }


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"thread_id": ""}, ValueError),
        ({"runnable": "chat"}, TypeError),
        ({"model": "test/scripted"}, TypeError),
        ({"policy": "default"}, TypeError),
        ({"request_id": ""}, ValueError),
    ],
)
def test_run_request_rejects_invalid_field_shapes(
    changes: dict[str, object],
    error: type[Exception],
) -> None:
    values: dict[str, object] = {
        "thread_id": "chat_test",
        "request_id": "request_1",
        "runnable": RunnableRequest("agic:chat", CallInput({"_": "hello"})),
        "model": ModelRequest(TEST_MODEL_REF),
        "policy": RunPolicy(),
    }

    with pytest.raises(error):
        RunRequest(**{**values, **changes})  # type: ignore[arg-type]


def test_rerun_request_rejects_ambiguous_inputs() -> None:
    rerun = RerunRequest(
        source="run_source",
        commands=(RunCommand("default", "model", "test/scripted"),),
        request_id="rerun_request",
    )

    assert {item.name for item in fields(RerunRequest)} == {
        "source",
        "commands",
        "request_id",
        "client_id",
        "model",
        "model_override",
    }
    with pytest.raises(ValueError, match="cannot replace the persisted runnable"):
        replace(
            rerun,
            commands=(RunCommand("default", "runnable", "agic:selected"),),
        )
    with pytest.raises(ValueError, match="cannot remove"):
        replace(rerun, commands=(), model_override=ModelOverride(identity="unset"))
    with pytest.raises(ValueError, match="cannot combine"):
        replace(
            rerun,
            commands=(),
            model=ModelRequest(TEST_MODEL_REF),
            model_override=ModelOverride(effort="high"),
        )


def test_local_client_resolves_fallback_input_and_policy_precedence(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=_CHAT_SOURCE,
        responses=[
            ModelCallResult(message=Message.assistant("fallback")),
            ModelCallResult(message=Message.assistant("session")),
            ModelCallResult(message=Message.assistant("selected")),
        ],
    )
    setup_reads = 0
    state_reads = 0
    include_setups: list[object] = []

    def setup():
        nonlocal setup_reads
        setup_reads += 1
        return harness.setup

    def state():
        nonlocal state_reads
        state_reads += 1
        return harness.state

    def include(current_setup: AgentSetup):
        include_setups.append(current_setup)
        return lambda reference: (
            TextPart("included")
            if reference == "note.md"
            else pytest.fail("unexpected include")
        )

    executor = RunExecutor(
        harness.store,
        harness.ids,
        setup=setup,
        state=state,
        include=include,
    )
    client: RunClient = LocalRunClient(executor)
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        await client.connect()
        thread = harness.threads.create(kind=ThreadKind.CHAT)
        fallback_handle = await client.run(
            _request(
                thread,
                input=CallInput({"_": "@note.md"}),
                request_id="fallback_request",
            ),
            tracer=tracer,
        )
        client_handle: RunHandle = fallback_handle
        fallback = await fallback_handle.wait()
        session_handle = await client.run(
            _request(
                thread,
                session_commands=(RunCommand("default", "runnable", "agic:session"),),
                request_id="session_request",
            )
        )
        session = await session_handle.wait()
        selected_handle = await client.run(
            _request(
                thread,
                commands=(RunCommand("default", "runnable", "agic:selected"),),
                session_commands=(RunCommand("default", "runnable", "agic:session"),),
                request_id="selected_request",
            )
        )
        selected = await selected_handle.wait()

        assert client_handle.run_id == fallback.id
        assert not isinstance(client_handle, LocalRunHandle)
        assert (
            fallback.runnable_name,
            session.runnable_name,
            selected.runnable_name,
        ) == (
            "chat",
            "session",
            "selected",
        )
        assert [item.status for item in (fallback, session, selected)] == [
            "succeeded",
            "succeeded",
            "succeeded",
        ]
        assert fallback.input_text == "@note.md"
        assert fallback.controls[0].request_id == "fallback_request"
        assert without_runtime_snapshots(
            harness.adapter.invocations[0].call.messages
        ) == [Message.user("included")]
        assert [event.type for event in tracer.events] == [
            "run_begin",
            "step_begin",
            "part_begin",
            "part_end",
            "step_end",
            "run_end",
        ]
        assert setup_reads == 3
        assert state_reads == 9
        assert include_setups == [harness.setup, harness.setup, harness.setup]

        await client.disconnect()

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_local_client_rejects_preparation_without_persisting_a_run(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(tmp_path, source=_CHAT_SOURCE, responses=[])
    client = LocalRunClient(harness.executor)

    async def scenario() -> None:
        await client.connect()
        thread = harness.threads.create(kind=ThreadKind.CHAT)
        request = _request(
            thread,
            commands=(RunCommand("default", "runnable", "agic:not_found"),),
        )

        with pytest.raises(ToolangError, match="Runnable not found: agic:not_found"):
            await client.run(request)

        assert harness.store.list_runs(thread_id=thread, limit=None) == []
        await client.disconnect()

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_executor_reruns_current_state_with_captured_prompt_input(
    tmp_path: Path,
) -> None:
    source_with_prompt = (
        """
prompt rewrite:
  Old {{style}} {{_}}

"""
        + _CHAT_SOURCE
    )
    changed_source = source_with_prompt.replace(
        "Old {{style}} {{_}}", "New {{style}} {{_}}"
    )
    harness = ExecutionHarness.create(
        tmp_path,
        prepare_state=True,
        source=source_with_prompt,
        responses=[
            RuntimeError("temporary failure"),
            ModelCallResult(message=Message.assistant("reran")),
            ModelCallResult(message=Message.assistant("resubmitted")),
        ],
    )
    from toolang.state.prepare import prepare_agent_state

    harness.setup.layout.program.write_text(changed_source, encoding="utf-8")
    changed_state = prepare_agent_state(harness.setup.layout)
    setup_reads = 0
    current_state_reads = 0

    def setup() -> AgentSetup:
        nonlocal setup_reads
        setup_reads += 1
        return harness.setup

    def current_state():
        nonlocal current_state_reads
        current_state_reads += 1
        return harness.state if current_state_reads == 1 else changed_state

    executor = RunExecutor(
        harness.store,
        harness.ids,
        setup=setup,
        state=current_state,
        include=lambda _setup: lambda _reference: TextPart("unused"),
    )
    client: RunClient = LocalRunClient(executor)
    rerun_tracer = RecordingRunTracer()

    async def scenario() -> None:
        await client.connect()
        thread = harness.threads.create(kind=ThreadKind.CHAT)
        source = await (
            await client.run(
                _request(
                    thread,
                    input=CallInput({"_": "$rewrite style=brief -- hello"}),
                    request_id="source_request",
                )
            )
        ).wait()
        assert source.status == "failed"

        rerun_handle = await client.rerun(
            RerunRequest(
                source=source.id,
                commands=(RunCommand("limit", "time", 30),),
                request_id="rerun_request",
            ),
            tracer=rerun_tracer,
        )
        rerun = await rerun_handle.wait()
        assert await rerun_handle.wait() == rerun
        resubmitted = await (
            await client.run(
                _request(
                    thread,
                    input=CallInput({"_": "$rewrite style=brief -- hello"}),
                    request_id="resubmit_request",
                )
            )
        ).wait()

        assert rerun_handle.run_id != source.id
        assert rerun.id == rerun_handle.run_id
        assert rerun.status == resubmitted.status == "succeeded"
        assert setup_reads == 3
        assert current_state_reads >= 3
        assert [
            without_runtime_snapshots(invocation.call.messages)
            for invocation in harness.adapter.invocations
        ] == [
            [Message.user("Old brief hello")],
            [Message.user("Old brief hello")],
            [Message.user("New brief hello")],
        ]
        assert [event.type for event in rerun_tracer.events][::5] == [
            "run_begin",
            "run_end",
        ]
        source_control = harness.store.get_run_control(run_id=source.id, index=0)
        rerun_control = harness.store.get_run_control(run_id=rerun.id, index=0)
        assert source_control is not None
        assert rerun_control is not None
        assert rerun_control.request_id == "rerun_request"
        assert rerun_control.payload.limits.time == 30
        assert (
            rerun_control.payload.authored_input
            == source_control.payload.authored_input
            == CallInput({"_": "$rewrite style=brief -- hello"})
        )
        assert (
            rerun_control.payload.prompt_invocations
            == source_control.payload.prompt_invocations
        )

        await client.disconnect()
        with pytest.raises(RuntimeError, match="run client is disconnected"):
            await client.rerun(
                RerunRequest(source=source.id, commands=(), request_id="disconnected")
            )

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_local_client_qualified_agic_fallback_skips_same_named_flow(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic relay(_: Part[]) -> Part[]:
  recall = none
  context = none
  instruct = none
  user: {{_}}

agic:
  recall = none
  context = none
  instruct = none
  user: {{_}}

flow chat(_: Part[]) -> Part[]:
  run relay
""",
        responses=[ModelCallResult(message=Message.assistant("default reply"))],
    )
    client = LocalRunClient(harness.executor)
    entry = next(name for name in harness.state.runnables if name.startswith("<entry:"))

    async def scenario() -> None:
        await client.connect()
        thread = harness.threads.create(kind=ThreadKind.CHAT)
        handle = await client.run(
            _request(
                thread,
                runnable_fallbacks=(f"agic:{entry}",),
            )
        )
        detail = await handle.wait()

        assert (detail.runnable_kind, detail.runnable_name) == ("agic", entry)
        assert len(harness.store.list_runs(thread_id=thread, limit=None)) == 1
        await client.disconnect()

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_local_client_returns_caller_facing_steer_and_cancel_controls(
    tmp_path: Path,
) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_CHAT_SOURCE,
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("unused")),
                gate=gate,
            )
        ],
    )
    client = LocalRunClient(harness.executor)

    async def scenario() -> None:
        await client.connect()
        thread = harness.threads.create(kind=ThreadKind.CHAT)
        handle = await client.run(_request(thread))
        await asyncio.wait_for(gate.wait_until_entered(), timeout=1)

        steer = await client.steer(
            handle.run_id,
            Message.user("new direction"),
            timing="next_call",
            request_id="steer_request",
        )
        cancellation = await client.cancel(
            handle.run_id,
            timing="immediate",
            request_id="cancel_request",
            reason="user canceled",
        )
        detail = await asyncio.wait_for(handle.wait(), timeout=2)

        assert (steer.kind, steer.timing, steer.request_id) == (
            "steer",
            "next_call",
            "steer_request",
        )
        assert isinstance(steer.payload, SteerControlPayload)
        assert tuple(cast(Array, steer.payload.input["_"])) == (
            TextPart("new direction"),
        )
        assert (cancellation.kind, cancellation.timing, cancellation.request_id) == (
            "cancel",
            "immediate",
            "cancel_request",
        )
        assert isinstance(cancellation.payload, CancelControlPayload)
        assert (TextPart(cast(str, cancellation.payload.input["_"])),) == (
            TextPart("user canceled"),
        )
        assert detail.status == "canceled"
        assert detail.error == ErrorMessage("user canceled")

        await client.disconnect()

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_local_client_disconnect_is_idempotent_without_stopping_executor(
    tmp_path: Path,
) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_CHAT_SOURCE,
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("unused")),
                gate=gate,
            )
        ],
    )
    client = LocalRunClient(harness.executor)

    async def scenario() -> None:
        thread = harness.threads.create(kind=ThreadKind.CHAT)
        with pytest.raises(RuntimeError, match="run client is disconnected"):
            await client.run(_request(thread, request_id="before_connect"))

        await client.connect()
        await client.connect()
        handle = await client.run(_request(thread))
        await asyncio.wait_for(gate.wait_until_entered(), timeout=1)

        await client.disconnect()
        await client.disconnect()
        stored = harness.store.get_run(run_id=handle.run_id)
        assert stored is not None
        assert stored.status == "running"
        with pytest.raises(RuntimeError, match="run client is disconnected"):
            await client.run(_request(thread, request_id="after_disconnect"))

        await client.connect()
        await client.cancel(handle.run_id, reason="owner canceled")
        detail = await asyncio.wait_for(handle.wait(), timeout=2)
        assert detail.status == "canceled"
        await client.disconnect()

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()
