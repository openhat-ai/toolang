from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.message import TextPart
from toolang.base.types.policy import RunBindings
from toolang.execution.calls import bind_runnable_call
from toolang.lang.submission import SettingCommand, parse_runnable_call
from toolang.execution.types import ThreadPrefix
from tests.support.execution_harness import ExecutionHarness


_SOURCE = """
agic default(_: Part[]):
  {{_}}

agic review(_: Part[], count: Number):
  {{_}}

agic bound(_: Part[]):
  {{_}}
"""


def test_bind_runnable_call_resolves_overrides_content_and_typed_args(
    tmp_path,
) -> None:
    harness = ExecutionHarness.create(tmp_path, source=_SOURCE, responses=[])
    try:
        spec = bind_runnable_call(
            parse_runnable_call(
                ":model test/scripted\n"
                ":agic review count=2\n\n"
                "Review this."
            ),
            setup=harness.setup,
            state=harness.state,
            thread="term_test",
            default_runnable="default",
        )

        assert spec.runnable == "review"
        assert spec.model == "test/scripted"
        assert spec.args == {"count": 2}
        assert spec.input == (TextPart("Review this."),)
        harness.executor.validate(spec)
    finally:
        harness.store.close()


def test_run_override_default_returns_to_surface_default_not_session_setting(
    tmp_path,
) -> None:
    harness = ExecutionHarness.create(tmp_path, source=_SOURCE, responses=[])
    try:
        spec = bind_runnable_call(
            parse_runnable_call(":agic default\nInput"),
            setup=harness.setup,
            state=harness.state,
            thread="term_test",
            default_runnable="default",
            settings=(
                SettingCommand(
                    kind="agic",
                    selector="review",
                    args=(("count", "2"),),
                ),
            ),
        )

        assert spec.runnable == "default"
        assert spec.args is None
    finally:
        harness.store.close()


def test_setup_bindings_are_below_session_and_authored_selections(tmp_path) -> None:
    harness = ExecutionHarness.create(tmp_path, source=_SOURCE, responses=[])
    setup = replace(
        harness.setup,
        bindings=RunBindings(model="test/scripted", runnable="agic:bound"),
    )
    try:
        bound = bind_runnable_call(
            parse_runnable_call("Input"),
            setup=setup,
            state=harness.state,
            thread="term_test",
            default_runnable="default",
        )
        session = bind_runnable_call(
            parse_runnable_call("Input"),
            setup=setup,
            state=harness.state,
            thread="term_test",
            default_runnable="default",
            settings=(
                SettingCommand(
                    kind="agic",
                    selector="review",
                    args=(("count", "2"),),
                ),
            ),
        )
        authored = bind_runnable_call(
            parse_runnable_call(":agic default\nInput"),
            setup=setup,
            state=harness.state,
            thread="term_test",
            default_runnable="default",
            settings=(
                SettingCommand(
                    kind="agic",
                    selector="review",
                    args=(("count", "2"),),
                ),
            ),
        )
        selected = bind_runnable_call(
            parse_runnable_call(":agic default\nInput"),
            setup=setup,
            state=harness.state,
            thread="term_test",
            default_runnable="default",
            selected_runnable="default",
            settings=(SettingCommand(kind="agic", selector="bound"),),
        )

        assert (bound.runnable, bound.model) == ("bound", "test/scripted")
        assert (session.runnable, session.args) == ("review", {"count": 2})
        assert authored.runnable == "bound"
        assert selected.runnable == "default"
    finally:
        harness.store.close()


def test_invalid_explicit_model_is_rejected_before_run_persistence(tmp_path) -> None:
    harness = ExecutionHarness.create(tmp_path, source=_SOURCE, responses=[])

    async def scenario() -> None:
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
        spec = bind_runnable_call(
            parse_runnable_call(":model missing\nInput"),
            setup=harness.setup,
            state=harness.state,
            thread=thread,
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
        spec = bind_runnable_call(
            parse_runnable_call("Input"),
            setup=setup,
            state=harness.state,
            thread=thread,
            default_runnable="default",
        )

        with pytest.raises(ToolangError, match="model"):
            harness.executor.start(spec)

        assert not harness.store.list_runs(thread_id=thread, limit=None)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()
