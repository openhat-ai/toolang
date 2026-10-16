"""Driver entry selection and captured runtime handlers use normal execution."""

import asyncio
from hashlib import sha256

import pytest

from toolang.base.types.message import Message, TextPart
from toolang.base.types.policy import RunBindings
from toolang.base.types.run import ModelCallResult
from toolang.common.errors import ToolangError
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.store import RunStore
from toolang.common.ids import IdIssuer
from toolang.execution.records import RunControlPayload
from toolang.execution.runnables import resolve_driver_handler
from toolang.execution.types import DriverName, ThreadPrefix
from toolang.lang import Program
from toolang.lang.input import CallInput
from toolang.state.state import compose_agent_state
from toolang.state.types import RUNTIME_TALK_MODULE
from tests.support.execution_harness import ExecutionHarness


def state_for(source):
    digest = sha256(source.encode()).hexdigest()
    return compose_agent_state(
        name="alice",
        root_revision=digest,
        home_revision=digest,
        root_config={},
        home_config={},
        root_caps=(),
        home_caps=(),
        modules={"agent": Program.from_source(source)},
        module_sources={"agent": "agent.too"},
        module_digests={"agent": digest},
        module_caps={"agent": ()},
    )


@pytest.mark.parametrize("driver", ["chat", "talk", "chore"])
@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_named_user_handler_precedes_unnamed_and_runtime(driver: DriverName, kind):
    state = state_for(f"{kind}:\n  pass\n{kind} {driver}:\n  pass\n")
    captured, target = resolve_driver_handler(state, driver)
    assert captured is state
    assert target.name == driver and target.executable.kind == kind
    assert target.module == "agent"


@pytest.mark.parametrize("driver", ["chat", "talk", "chore"])
@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_invocation_can_select_unnamed_or_unrelated_entry(driver: DriverName, kind):
    state = state_for(
        f"{kind}:\n  pass\n{kind} work:\n  pass\n{kind} {driver}:\n  pass\n"
    )
    assert resolve_driver_handler(state, driver, "_")[1].name.startswith("<entry:")
    assert resolve_driver_handler(state, driver, f"{kind}:work")[1].name == "work"


@pytest.mark.parametrize("driver", ["chat", "talk", "chore"])
@pytest.mark.parametrize("reference", ["missing", "", "agic:work"])
def test_invalid_invocation_never_uses_another_handler(driver: DriverName, reference):
    state = state_for(f"flow work:\n  pass\nagic {driver}:\n  pass\nagic:\n  pass\n")
    with pytest.raises((ToolangError, ValueError)):
        resolve_driver_handler(state, driver, reference)


@pytest.mark.parametrize("driver", ["chat", "chore"])
def test_no_runtime_chat_or_chore_handler(driver: DriverName):
    state = state_for("flow:\n  pass\n")
    assert resolve_driver_handler(state, driver)[1].name.startswith("<entry:")
    with pytest.raises(ToolangError, match="or unnamed entry"):
        resolve_driver_handler(state_for("flow other:\n  pass\n"), driver)


@pytest.mark.parametrize("source", ["", "flow:\n  pass\n"])
def test_runtime_talk_precedes_unnamed_without_changing_user_state(source):
    state = state_for(source)
    captured, target = resolve_driver_handler(state, "talk")
    assert target.qualified == f"{RUNTIME_TALK_MODULE}::agic:talk"
    assert captured.revision == state.revision
    assert captured.runnables == state.runnables
    assert captured.caps_for(target.module) == state.caps_for("agent")
    assert not state.runtime_sources and RUNTIME_TALK_MODULE not in state.modules


def test_runtime_talk_source_is_recorded_and_reused_by_rerun(tmp_path, monkeypatch):
    harness = ExecutionHarness.create(
        tmp_path,
        source="flow:\n  let fallback = unused\n",
        prepare_state=True,
        responses=[ModelCallResult(message=Message.assistant("Handled."))] * 2,
    )

    async def scenario():
        async with harness:
            spec = RunSpec(
                setup=harness.setup,
                state=harness.state,
                thread=harness.threads.create(prefix=ThreadPrefix.TALK),
                bindings=RunBindings(),
                limits=harness.setup.limits,
                driver="talk",
                input=CallInput({"_": (TextPart('{"messages": []}'),)}),
            )
            run = await harness.executor.run(spec)
            assert run.status == "succeeded", run.error
            control = harness.store.get_run_control(run_id=run.id, index=0)
            assert control is not None and isinstance(
                control.payload, RunControlPayload
            )
            assert control.payload.runnable == f"{RUNTIME_TALK_MODULE}::agic:talk"
            source = control.payload.runtime_sources[RUNTIME_TALK_MODULE]
            assert "convo__write" in source
            assert dict(control.payload.input) == {"_": {"messages": ()}}
            assert len(harness.adapter.invocations) == 1
            monkeypatch.setattr(
                "toolang.execution.runnables.talk_handler_source",
                lambda: pytest.fail("rerun must use the recorded runtime source"),
            )
            reopened = RunStore(harness.setup.layout.run_store)
            restarted = RunExecutor(
                reopened, IdIssuer(harness.setup.layout.run_store, agent_name="alice")
            )
            try:
                second = await restarted.rerun(
                    run.id, setup=harness.setup, state=harness.state
                )
                assert second.status == "succeeded", second.error
                restored = reopened.get_run_control(run_id=second.id, index=0)
                assert restored is not None and isinstance(
                    restored.payload, RunControlPayload
                )
                assert restored.payload.runtime_sources[RUNTIME_TALK_MODULE] == source
                assert not harness.state.runtime_sources
            finally:
                await restarted.stop()
                reopened.close()

    asyncio.run(scenario())


def test_selected_handler_bad_input_does_not_fall_back(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="flow chat(count: Number):\n  pass\nflow:\n  pass\n",
        responses=[],
    )

    async def scenario():
        async with harness:
            spec = RunSpec(
                setup=harness.setup,
                state=harness.state,
                thread=harness.threads.create(prefix=ThreadPrefix.CHAT),
                bindings=RunBindings(),
                limits=harness.setup.limits,
                driver="chat",
                input=CallInput({"count": (TextPart("invalid"),)}),
            )
            with pytest.raises((ToolangError, ValueError)):
                harness.executor.run(spec)
            assert harness.store.list_runs() == []

    asyncio.run(scenario())


def test_chat_session_can_choose_an_entry_without_a_default_handler(tmp_path):
    from toolang.cli.toolang.commands.chat.local import LocalChatSession
    from toolang.cli.toolang.commands.chat.policy import (
        build_run_request,
        update_session_setting,
    )
    from toolang.execution.types import RunOverride

    harness = ExecutionHarness.create(
        tmp_path,
        source="flow work(_: Text) -> Text:\n  pass\n",
        responses=[],
    )
    automatic = LocalChatSession._current_session_setting(
        setup=harness.setup, state=harness.state
    )
    assert automatic.runnable is None and automatic.runnable_follows_default
    selected = update_session_setting(
        surface=automatic,
        current=automatic,
        update=RunOverride(runnable="flow:work"),
    )
    assert automatic.runnable is None
    assert (
        resolve_driver_handler(harness.state, "talk")[1].module == RUNTIME_TALK_MODULE
    )
    with pytest.raises(ToolangError, match="chore"):
        resolve_driver_handler(harness.state, "chore")

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.CHAT)
            request = build_run_request(
                thread_id=thread,
                request_id="session_selected",
                input=CallInput({"_": "hello"}),
                override=RunOverride(),
                setting=selected,
                surface=automatic,
                resolve_model_ref=lambda value: value,
                resolve_runnable_ref=lambda value: value,
            )
            result = await harness.executor.run(request)
            assert result.status == "succeeded", result.error
            reset = update_session_setting(
                surface=automatic,
                current=selected,
                update=RunOverride(runnable="default"),
            )
            request = build_run_request(
                thread_id=thread,
                request_id="session_reset",
                input=CallInput({"_": "hello"}),
                override=RunOverride(),
                setting=reset,
                surface=automatic,
                resolve_model_ref=lambda value: value,
                resolve_runnable_ref=lambda value: value,
            )
            assert request.runnable.ref is None
            with pytest.raises(ToolangError, match="chat"):
                harness.executor.run(request)
            assert len(harness.store.list_runs()) == 1
            assert selected.runnable == "flow:work"

    asyncio.run(scenario())


def test_handler_resolution_precedes_model_resource_validation(tmp_path):
    from toolang.base.types.model import ModelRequest
    from toolang.base.types.policy import RunPolicy
    from toolang.execution.calls import resolve_run_request
    from toolang.execution.schemas import RunRequest, RunnableRequest

    harness = ExecutionHarness.create(
        tmp_path, source="flow other:\n  pass\n", responses=[]
    )
    try:
        request = RunRequest(
            thread_id="chat_test",
            request_id="invalid_handler_and_model",
            runnable=RunnableRequest(None),
            driver="chat",
            model=ModelRequest("test/missing"),
            policy=RunPolicy(),
        )
        with pytest.raises(ToolangError, match="no chat entry"):
            resolve_run_request(request, setup=harness.setup, state=harness.state)
    finally:
        harness.store.close()
