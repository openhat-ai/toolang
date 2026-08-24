from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.message import TextPart
from toolang.base.types.policy import RunBindings
from toolang.execution.calls import parse_call, resolve_spec
from toolang.execution.types import RunOverride, ThreadPrefix
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
            space="collab",
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
            space="collab",
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
        named: tuple[tuple[str, str], ...] = (),
    ):
        commands, input = parse_call(source)
        return resolve_spec(
            commands,
            input,
            setup=setup,
            state=harness.state,
            thread="term_test",
            space="collab",
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
            named=(("count", "2"),),
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
            space="collab",
            default_runnable="default",
        )

        with pytest.raises(ToolangError, match="model"):
            harness.executor.start(spec)

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
            space="collab",
            default_runnable="default",
        )

        with pytest.raises(ToolangError, match="model"):
            harness.executor.start(spec)

        assert not harness.store.list_runs(thread_id=thread, limit=None)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()
