"""Run client boundary and process-local adapter behavior."""

from __future__ import annotations

import asyncio
from dataclasses import fields
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
from toolang.base.types.run import ModelCallResult
from toolang.execution.client import LocalRunClient, RunClient, RunHandle
from toolang.execution.executor import LocalRunHandle
from toolang.execution.records import SteerControlPayload, CancelControlPayload
from toolang.execution.schemas import RunRequest
from toolang.execution.types import RunOverride, ThreadPrefix
from toolang.execution.values import parts_from_local
from toolang.lang.input import RunnableInputRaw
from toolang.setup import AgentSetup


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

    client: RunClient = LocalRunClient(
        harness.executor,
        setup=setup,
        state=state,
        include=include,
    )
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
        assert fallback.input_text == "included"
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
    client = LocalRunClient(
        harness.executor,
        setup=lambda: harness.setup,
        state=lambda: harness.state,
        include=lambda _setup: lambda _reference: TextPart("unused"),
    )

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
    client = LocalRunClient(
        harness.executor,
        setup=lambda: harness.setup,
        state=lambda: harness.state,
        include=lambda _setup: lambda _reference: TextPart("unused"),
    )

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
    client = LocalRunClient(
        harness.executor,
        setup=lambda: harness.setup,
        state=lambda: harness.state,
        include=lambda _setup: lambda _reference: TextPart("unused"),
    )

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
    client = LocalRunClient(
        harness.executor,
        setup=lambda: harness.setup,
        state=lambda: harness.state,
        include=lambda _setup: lambda _reference: TextPart("unused"),
    )

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
