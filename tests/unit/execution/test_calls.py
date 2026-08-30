from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.message import Message, TextPart
from toolang.base.types.model import (
    ModelParameters,
    ModelRequest,
    ReasoningParameters,
)
from toolang.base.types.run import ModelCallResult
from toolang.base.types.policy import RunBindings, RunPolicy
from toolang.execution.calls import (
    parse_call,
    resolve_restart_request,
    resolve_run_request,
    resolve_spec,
)
from toolang.execution.schemas import (
    RerunRequest,
    RetryRequest,
    RunRequest,
    RunnableRequest,
)
from toolang.execution.types import RunOverride, ThreadPrefix
from toolang.lang.input import NamedInputSource, NamedInputSources, RunnableInputRaw
from toolang.lang.types import Array
from tests.support.execution_harness import ExecutionHarness


_SOURCE = """
agic default(_: Part[]):
  {{_}}

agic review(_: Part[], count: Number):
  {{_}}

agic bound(_: Part[]):
  {{_}}
"""


def test_restart_resolution_preserves_model_unless_rerun_replaces_it(
    tmp_path,
) -> None:
    harness = ExecutionHarness.create(tmp_path, source=_SOURCE, responses=[])
    explicit = ModelRequest(
        "test/scripted",
        ModelParameters(ReasoningParameters("high")),
    )
    try:
        preserved = resolve_restart_request(
            RerunRequest("run_source", (), "rerun_preserved"),
            setup=harness.setup,
            state=harness.state,
        )
        replaced = resolve_restart_request(
            RerunRequest("run_source", (), "rerun_replaced", model=explicit),
            setup=harness.setup,
            state=harness.state,
        )
        legacy = resolve_restart_request(
            RerunRequest(
                "run_source",
                (RunOverride("default", "model", "test/scripted"),),
                "rerun_legacy",
            ),
            setup=harness.setup,
            state=harness.state,
        )

        assert preserved.model is None
        assert replaced.model == explicit
        assert legacy.model == ModelRequest("test/scripted")
        with pytest.raises(ValueError, match="retry request cannot replace"):
            RetryRequest(
                "run_source",
                (RunOverride("default", "model", "test/scripted"),),
                "retry_replacement",
            )
    finally:
        harness.store.close()


def test_materialized_agic_request_requires_a_model(tmp_path) -> None:
    harness = ExecutionHarness.create(tmp_path, source=_SOURCE, responses=[])
    try:
        with pytest.raises(ValueError, match="requires a model"):
            resolve_run_request(
                RunRequest(
                    thread_id="term_test",
                    request_id="request_without_model",
                    runnable=RunnableRequest(
                        "agic:default",
                        RunnableInputRaw(_="hello"),
                    ),
                    model=None,
                    policy=RunPolicy(),
                ),
                setup=harness.setup,
                state=harness.state,
            )
    finally:
        harness.store.close()


def test_root_runnable_selector_is_removed_from_current_model_input(tmp_path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow hello_flow(_: Text) -> Text:
  run worker
""",
        responses=(ModelCallResult(message=Message.assistant("done")),),
    )

    async def scenario() -> None:
        async with harness:
            commands, input = parse_call(":flow hello_flow\n\nhello world")
            root = await harness.executor.run(
                resolve_spec(
                    commands,
                    input,
                    setup=harness.setup,
                    state=harness.state,
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    default_runnable="default",
                )
            )

            assert root.status == "succeeded", root.error
            assert harness.adapter.invocations[0].call.messages[-1] == Message.user(
                "hello world"
            )

    asyncio.run(scenario())


def test_root_runnable_selector_is_removed_from_recalled_history(tmp_path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

agic chat(_: Text) -> Text:
  recall = history
  context: none
  instruct: none
  user: {{_}}

flow hello_flow(_: Text) -> Text:
  run worker
""",
        responses=(
            ModelCallResult(message=Message.assistant("first done")),
            ModelCallResult(message=Message.assistant("second done")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            commands, input = parse_call(":flow hello_flow\n\nhello world")
            first = await harness.executor.run(
                resolve_spec(
                    commands,
                    input,
                    setup=harness.setup,
                    state=harness.state,
                    thread=thread,
                    default_runnable="default",
                )
            )
            commands, input = parse_call(":agic chat\n\nnext")
            second = await harness.executor.run(
                resolve_spec(
                    commands,
                    input,
                    setup=harness.setup,
                    state=harness.state,
                    thread=thread,
                    default_runnable="default",
                )
            )

            assert first.status == second.status == "succeeded"
            assert harness.adapter.invocations[1].call.messages == [
                Message.user("hello world"),
                Message.user("hello world"),
                Message.assistant("first done"),
                Message.user("next"),
            ]

    asyncio.run(scenario())


def test_resolve_spec_binds_policy_primary_and_typed_named_inputs(
    tmp_path,
) -> None:
    harness = ExecutionHarness.create(tmp_path, source=_SOURCE, responses=[])
    try:
        commands, input = parse_call(
            ":model test/scripted\n:agic review count=2\n\nReview this."
        )
        spec = resolve_spec(
            commands,
            input,
            setup=harness.setup,
            state=harness.state,
            thread="term_test",
            default_runnable="default",
        )

        assert spec.bindings == RunBindings(
            model="test/scripted",
            runnable="agic:review",
        )
        assert spec.input.named == {"count": 2}
        assert spec.input.primary == Array("Part[]", (TextPart("Review this."),))
        harness.executor.validate(spec)
    finally:
        harness.store.close()


def test_resolve_spec_preserves_authored_prompt_input_and_provenance(tmp_path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
prompt review:
  params = focus

  {{focus}} {{_}}

agic default(_: Part[]):
  {{_}}
""",
        responses=[],
    )
    try:
        commands, input = parse_call(
            ":limit time=30\n\n$review focus=security -- inspect this"
        )
        spec = resolve_spec(
            commands,
            input,
            setup=harness.setup,
            state=harness.state,
            thread="term_test",
            default_runnable="default",
        )

        assert spec.input.primary == Array(
            "Part[]", (TextPart("security inspect this"),)
        )
        assert spec.authored_input == RunnableInputRaw(
            _="$review focus=security -- inspect this"
        )
        assert spec.authored_commands == (RunOverride("limit", "time", 30),)
        assert len(spec.prompt_invocations) == 1
        invocation = spec.prompt_invocations[0]
        assert invocation.name == "review"
        assert invocation.arguments == (("focus", "security"),)
        assert invocation.input_scope == "inline"
        assert invocation.parent is None
        assert invocation.cap_ref
        assert len(invocation.content_hash) == 64
    finally:
        harness.store.close()


def test_run_default_returns_to_surface_binding_not_session_binding(
    tmp_path,
) -> None:
    harness = ExecutionHarness.create(tmp_path, source=_SOURCE, responses=[])
    try:
        commands, input = parse_call(":agic default\nInput")
        spec = resolve_spec(
            commands,
            input,
            setup=harness.setup,
            state=harness.state,
            thread="term_test",
            default_runnable="default",
            surface=RunBindings(runnable="agic:default"),
            session_commands=(RunOverride("default", "runnable", "agic:review"),),
        )

        assert spec.bindings.runnable == "agic:default"
        assert spec.input.named == {}
    finally:
        harness.store.close()


def test_setup_bindings_are_below_surface_session_and_run_selections(
    tmp_path,
) -> None:
    harness = ExecutionHarness.create(tmp_path, source=_SOURCE, responses=[])
    setup = replace(
        harness.setup,
        bindings=RunBindings(model="test/scripted", runnable="agic:bound"),
    )

    def resolve(
        source: str,
        *,
        surface: RunBindings = RunBindings(),
        session: tuple[RunOverride, ...] = (),
        named: NamedInputSources = (),
    ):
        commands, input = parse_call(source)
        return resolve_spec(
            commands,
            input,
            setup=setup,
            state=harness.state,
            thread="term_test",
            default_runnable="default",
            surface=surface,
            session_commands=session,
            surface_named_sources=named,
        )

    try:
        bound = resolve("Input")
        session = resolve(
            "Input",
            session=(RunOverride("default", "runnable", "agic:review"),),
            named=(NamedInputSource("count", "2"),),
        )
        authored = resolve(
            ":agic default\nInput",
            session=(RunOverride("default", "runnable", "agic:review"),),
        )
        selected = resolve(
            ":agic default\nInput",
            surface=RunBindings(runnable="agic:default"),
            session=(RunOverride("default", "runnable", "agic:bound"),),
        )

        assert bound.bindings == RunBindings(
            model="test/scripted",
            runnable="agic:bound",
        )
        assert (session.bindings.runnable, session.input.named) == (
            "agic:review",
            {"count": 2},
        )
        assert authored.bindings.runnable == "agic:bound"
        assert selected.bindings.runnable == "agic:default"
    finally:
        harness.store.close()


def test_invalid_explicit_model_is_rejected_before_run_persistence(tmp_path) -> None:
    harness = ExecutionHarness.create(tmp_path, source=_SOURCE, responses=[])

    async def scenario() -> None:
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
        commands, input = parse_call(":model missing\nInput")
        spec = resolve_spec(
            commands,
            input,
            setup=harness.setup,
            state=harness.state,
            thread=thread,
            default_runnable="default",
        )

        with pytest.raises(ToolangError, match="model"):
            harness.executor.run(spec)

        assert not harness.store.list_runs(thread_id=thread, limit=None)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_missing_default_model_is_rejected_before_run_persistence(tmp_path) -> None:
    harness = ExecutionHarness.create(tmp_path, source=_SOURCE, responses=[])
    setup = harness.setup.__class__(
        layout=harness.setup.layout,
        providers={},
        adapters={},
        models=(),
        tools={},
        envs={},
        environment=harness.setup.environment,
    )

    async def scenario() -> None:
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
        commands, input = parse_call("Input")
        spec = resolve_spec(
            commands,
            input,
            setup=setup,
            state=harness.state,
            thread=thread,
            default_runnable="default",
        )

        with pytest.raises(ToolangError, match="model"):
            harness.executor.run(spec)

        assert not harness.store.list_runs(thread_id=thread, limit=None)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()
