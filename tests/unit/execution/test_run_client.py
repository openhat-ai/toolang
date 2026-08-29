"""Run client boundary and process-local adapter behavior."""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from hashlib import sha256
from pathlib import Path

import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.errors import ToolangError
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult, ModelUsage
from toolang.execution.client import LocalRunClient, RunClient, RunHandle
from toolang.execution.executor import LocalRunHandle, RunExecutor
from toolang.execution.records import SteerControlPayload, CancelControlPayload
from toolang.execution.schemas import RerunRequest, RetryRequest, RunRequest
from toolang.execution.types import RunOverride, StepPath, ThreadPrefix
from toolang.execution.values import parts_from_local
from toolang.lang import Program
from toolang.lang.input import RunnableInputRaw
from toolang.setup import AgentSetup
from toolang.state.state import agent_state_revision


_CHAT_SOURCE = """
agic chat(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

agic session(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

agic selected(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
"""


def _request(
    thread: str,
    *,
    commands: tuple[RunOverride, ...] = (),
    session_commands: tuple[RunOverride, ...] = (),
    input: RunnableInputRaw = RunnableInputRaw(primary="hello"),
    runnable_fallbacks: tuple[str, ...] = ("missing", "chat", "default"),
    request_id: str = "request_1",
) -> RunRequest:
    return RunRequest(
        thread=thread,
        commands=commands,
        input=input,
        session_commands=session_commands,
        runnable_fallbacks=runnable_fallbacks,
        request_id=request_id,
    )


def test_run_request_contains_only_unresolved_caller_values() -> None:
    request = _request("term_test")

    assert request == RunRequest(
        thread="term_test",
        commands=(),
        input=RunnableInputRaw(primary="hello"),
        session_commands=(),
        runnable_fallbacks=("missing", "chat", "default"),
        request_id="request_1",
    )
    assert {item.name for item in fields(RunRequest)} == {
        "thread",
        "commands",
        "input",
        "session_commands",
        "runnable_fallbacks",
        "request_id",
    }


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"thread": ""}, ValueError),
        ({"commands": []}, TypeError),
        ({"commands": ("invalid",)}, TypeError),
        ({"input": "hello"}, TypeError),
        ({"session_commands": []}, TypeError),
        ({"runnable_fallbacks": ()}, ValueError),
        ({"runnable_fallbacks": ("chat", "chat")}, ValueError),
        ({"runnable_fallbacks": (" chat",)}, ValueError),
        ({"request_id": ""}, ValueError),
    ],
)
def test_run_request_rejects_invalid_field_shapes(
    changes: dict[str, object],
    error: type[Exception],
) -> None:
    values: dict[str, object] = {
        "thread": "term_test",
        "commands": (),
        "input": RunnableInputRaw(primary="hello"),
        "session_commands": (),
        "runnable_fallbacks": ("chat", "default"),
        "request_id": "request_1",
    }

    with pytest.raises(error):
        RunRequest(**{**values, **changes})  # type: ignore[arg-type]


def test_restart_requests_keep_retry_and_rerun_inputs_unambiguous() -> None:
    retry = RetryRequest(
        source="run_source",
        commands=(RunOverride("limit", "tokens", 10),),
        request_id="retry_request",
        anchor=StepPath("run_source", (1,)),
    )
    rerun = RerunRequest(
        source="run_source",
        commands=(RunOverride("default", "model", "test/scripted"),),
        request_id="rerun_request",
    )

    assert {item.name for item in fields(RetryRequest)} == {
        "source",
        "commands",
        "request_id",
        "anchor",
    }
    assert {item.name for item in fields(RerunRequest)} == {
        "source",
        "commands",
        "request_id",
    }
    assert retry.anchor == StepPath("run_source", (1,))
    assert rerun.source == retry.source

    with pytest.raises(ValueError, match="anchor must belong"):
        replace(retry, anchor=StepPath("run_other", (1,)))
    with pytest.raises(ValueError, match="cannot replace the persisted runnable"):
        replace(
            rerun,
            commands=(RunOverride("default", "runnable", "agic:selected"),),
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
        load_state=lambda _revision: harness.state,
        include=include,
    )
    client: RunClient = LocalRunClient(executor)
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        await client.connect()
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
        fallback_handle = await client.run(
            _request(
                thread,
                input=RunnableInputRaw(primary="@note.md"),
                request_id="fallback_request",
            ),
            tracer=tracer,
        )
        client_handle: RunHandle = fallback_handle
        fallback = await fallback_handle.wait()
        session_handle = await client.run(
            _request(
                thread,
                session_commands=(RunOverride("default", "runnable", "agic:session"),),
                request_id="session_request",
            )
        )
        session = await session_handle.wait()
        selected_handle = await client.run(
            _request(
                thread,
                commands=(RunOverride("default", "runnable", "agic:selected"),),
                session_commands=(RunOverride("default", "runnable", "agic:session"),),
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
        assert harness.adapter.invocations[0].call.messages == [
            Message.user("included")
        ]
        assert [event.type for event in tracer.events] == [
            "run_begin",
            "step_begin",
            "part_begin",
            "part_end",
            "step_end",
            "run_end",
        ]
        assert setup_reads == 3
        assert state_reads == 3
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
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
        request = _request(
            thread,
            commands=(RunOverride("default", "runnable", "agic:not_found"),),
        )

        with pytest.raises(ToolangError, match="Runnable not found"):
            await client.run(request)

        assert harness.store.list_runs(thread_id=thread, limit=None) == []
        await client.disconnect()

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_executor_retries_recorded_state_and_reruns_current_state(
    tmp_path: Path,
) -> None:
    source_with_prompt = (
        """
prompt rewrite:
  params = style

  Old {{style}} {{_}}

"""
        + _CHAT_SOURCE
    )
    changed_source = source_with_prompt.replace(
        "Old {{style}} {{_}}", "New {{style}} {{_}}"
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=source_with_prompt,
        responses=[
            RuntimeError("temporary failure"),
            ModelCallResult(
                message=Message.assistant("recovered"),
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
            ModelCallResult(message=Message.assistant("reran")),
            ModelCallResult(message=Message.assistant("resubmitted")),
        ],
    )
    changed_home_revision = sha256(changed_source.encode("utf-8")).hexdigest()
    changed_state = replace(
        harness.state,
        revision=agent_state_revision(
            harness.state.root_revision,
            changed_home_revision,
        ),
        home_revision=changed_home_revision,
        modules={"agent": Program.from_source(changed_source)},
        module_digests={"agent": changed_home_revision},
    )
    setup_reads = 0
    current_state_reads = 0
    loaded_revisions: list[str] = []

    def setup() -> AgentSetup:
        nonlocal setup_reads
        setup_reads += 1
        return harness.setup

    def current_state():
        nonlocal current_state_reads
        current_state_reads += 1
        return harness.state if current_state_reads == 1 else changed_state

    def load_state(revision: str):
        loaded_revisions.append(revision)
        assert revision == harness.state.revision
        return harness.state

    executor = RunExecutor(
        harness.store,
        harness.ids,
        setup=setup,
        state=current_state,
        load_state=load_state,
        include=lambda _setup: lambda _reference: TextPart("unused"),
    )
    client: RunClient = LocalRunClient(executor)
    retry_tracer = RecordingRunTracer()
    rerun_tracer = RecordingRunTracer()

    async def scenario() -> None:
        await client.connect()
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
        source = await (
            await client.run(
                _request(
                    thread,
                    input=RunnableInputRaw(primary="$rewrite style=brief -- hello"),
                    request_id="source_request",
                ),
            )
        ).wait()
        assert source.status == "failed"

        retry_handle = await client.retry(
            RetryRequest(
                source=source.id,
                commands=(RunOverride("limit", "tokens", 10),),
                request_id="retry_request",
            ),
            tracer=retry_tracer,
        )
        retried = await retry_handle.wait()
        assert await retry_handle.wait() == retried

        rerun_handle = await client.rerun(
            RerunRequest(
                source=source.id,
                commands=(RunOverride("limit", "time", 30),),
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
                    input=RunnableInputRaw(primary="$rewrite style=brief -- hello"),
                    request_id="resubmit_request",
                )
            )
        ).wait()

        assert retry_handle.run_id == source.id == retried.id
        assert rerun_handle.run_id != source.id
        assert rerun.id == rerun_handle.run_id
        assert retried.status == rerun.status == resubmitted.status == "succeeded"
        assert setup_reads == 4
        assert current_state_reads == 3
        assert loaded_revisions == [harness.state.revision]
        assert [
            invocation.call.messages for invocation in harness.adapter.invocations
        ] == [
            [Message.user("Old brief hello")],
            [Message.user("Old brief hello")],
            [Message.user("Old brief hello")],
            [Message.user("New brief hello")],
        ]
        assert [event.type for event in retry_tracer.events][::5] == [
            "run_begin",
            "run_end",
        ]
        assert [event.type for event in rerun_tracer.events][::5] == [
            "run_begin",
            "run_end",
        ]
        retry_control = harness.store.list_run_controls(run_id=source.id)[-1]
        source_control = harness.store.get_run_control(run_id=source.id, index=0)
        rerun_control = harness.store.get_run_control(run_id=rerun.id, index=0)
        assert source_control is not None
        assert retry_control.request == "retry_request"
        assert retry_control.payload.limits.tokens == 10
        assert rerun_control is not None
        assert rerun_control.request == "rerun_request"
        assert rerun_control.payload.limits.time == 30
        assert (
            retry_control.payload.authored_input
            == rerun_control.payload.authored_input
            == source_control.payload.authored_input
            == RunnableInputRaw(primary="$rewrite style=brief -- hello")
        )
        assert (
            retry_control.payload.prompt_invocations
            == rerun_control.payload.prompt_invocations
            == source_control.payload.prompt_invocations
        )

        await client.disconnect()
        with pytest.raises(RuntimeError, match="run client is disconnected"):
            await client.retry(
                RetryRequest(source=source.id, commands=(), request_id="disconnected")
            )
        with pytest.raises(RuntimeError, match="run client is disconnected"):
            await client.rerun(
                RerunRequest(source=source.id, commands=(), request_id="disconnected")
            )

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_executor_rejects_retry_when_recorded_state_is_unavailable_without_mutation(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=_CHAT_SOURCE,
        responses=[RuntimeError("temporary failure")],
    )

    def missing_state(revision: str):
        raise FileNotFoundError(revision)

    executor = RunExecutor(
        harness.store,
        harness.ids,
        setup=lambda: harness.setup,
        state=lambda: harness.state,
        load_state=missing_state,
        include=lambda _setup: lambda _reference: TextPart("unused"),
    )
    client = LocalRunClient(executor)

    async def scenario() -> None:
        await client.connect()
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
        source = await (
            await client.run(_request(thread, request_id="source_request"))
        ).wait()
        assert source.status == "failed"
        before_run = harness.store.get_run(run_id=source.id)
        before_controls = harness.store.list_run_controls(run_id=source.id)
        before_steps = harness.store.list_steps(
            run_id=source.id,
            include_ejected=True,
        )

        with pytest.raises(
            ValueError,
            match="retry state snapshot is not available",
        ):
            await client.retry(
                RetryRequest(
                    source=source.id,
                    commands=(),
                    request_id="retry_request",
                )
            )

        assert harness.store.get_run(run_id=source.id) == before_run
        assert harness.store.list_run_controls(run_id=source.id) == before_controls
        assert (
            harness.store.list_steps(
                run_id=source.id,
                include_ejected=True,
            )
            == before_steps
        )
        await client.disconnect()

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_local_client_rejects_retry_on_a_different_sandbox_without_mutation(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=_CHAT_SOURCE,
        responses=[RuntimeError("temporary failure")],
    )
    current_setup = [harness.setup]
    executor = RunExecutor(
        harness.store,
        harness.ids,
        setup=lambda: current_setup[0],
        state=lambda: harness.state,
        load_state=lambda _revision: harness.state,
        include=lambda _setup: lambda _reference: TextPart("unused"),
    )
    client = LocalRunClient(executor)

    async def scenario() -> None:
        await client.connect()
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
        source = await (
            await client.run(_request(thread, request_id="source_request"))
        ).wait()
        environment = harness.setup.environment
        assert environment is not None
        current_setup[0] = replace(
            harness.setup,
            environment=replace(
                environment,
                sandbox="docker:python:3.13-slim",
                container=True,
            ),
        )

        with pytest.raises(ValueError, match="does not match original sandbox"):
            await client.retry(
                RetryRequest(
                    source=source.id,
                    commands=(),
                    request_id="retry_request",
                )
            )

        stored = harness.store.get_run(run_id=source.id)
        assert stored is not None and stored.status == "failed"
        assert len(harness.store.list_run_controls(run_id=source.id)) == 1
        await client.disconnect()

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
  context: none
  instruct: none
  user: {{_}}

flow chat(_: Part[]) -> Part[]:
  run relay
""",
        responses=[ModelCallResult(message=Message.assistant("default reply"))],
    )
    client = LocalRunClient(harness.executor)

    async def scenario() -> None:
        await client.connect()
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
        handle = await client.run(
            _request(
                thread,
                runnable_fallbacks=("agic:chat", "default"),
            )
        )
        detail = await handle.wait()

        assert (detail.runnable_kind, detail.runnable_name) == ("agic", "default")
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
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
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
        assert parts_from_local(steer.payload.locals[0]) == (TextPart("new direction"),)
        assert (cancellation.kind, cancellation.timing, cancellation.request_id) == (
            "cancel",
            "immediate",
            "cancel_request",
        )
        assert isinstance(cancellation.payload, CancelControlPayload)
        assert parts_from_local(cancellation.payload.locals[0]) == (
            TextPart("user canceled"),
        )
        assert detail.status == "canceled"
        assert detail.error == "user canceled"

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
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
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
