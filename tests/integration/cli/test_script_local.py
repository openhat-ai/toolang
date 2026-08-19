from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.cli.toolang.commands import script
from toolang.execution.store import RunStore
from toolang.execution.records import StartControlPayload
from toolang.up import process as agents
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.execution.types import ThreadPrefix
from toolang.lang.input import resolve_input_parts


_SOURCE = """
agic echo(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
"""

_FLOW_SOURCE = """
agic expand(_: Part[]) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: Return exactly two short JSON strings for {{_}}.

flow research(_: Part[]) -> Text[]:
  ## Expand the topic.
  scatter 2 expand
"""


def test_local_script_runs_through_execution_and_persists_script_thread(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source = tmp_path / "echo.too"
    source.write_text(_SOURCE, encoding="utf-8")
    layout = agents.materialize_roaming_program(source)
    harness = ExecutionHarness.create(
        tmp_path / "harness",
        source=_SOURCE,
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    setup = replace(harness.setup, layout=layout)

    class _SetupWatcher:
        def __init__(self, actual_layout, **_kwargs) -> None:
            assert actual_layout == layout

        async def refresh(self):
            return setup

    monkeypatch.setattr(script, "SetupWatcher", _SetupWatcher)
    monkeypatch.setattr(
        script,
        "prepare_agent_state",
        lambda actual_layout, **_kwargs: (
            harness.state
            if actual_layout == layout
            else pytest.fail("unexpected layout")
        ),
    )
    monkeypatch.setattr(script, "configure_logging_plan", lambda _plan: None)

    result = script.dispatch(
        [],
        [str(source), "echo", "hello"],
        prog_name="toolang",
    )
    output = capsys.readouterr()

    assert result == 0
    assert output.out == "done\n"
    assert output.err == ""
    store = RunStore(layout.run_store)
    try:
        threads = store.list_threads()
        runs = store.list_runs(thread_id=threads[0].thread_id, limit=None)
        control = store.get_run_control(run_id=runs[0].id, index=0)
    finally:
        store.close()
        asyncio.run(harness.executor.shutdown())
        harness.store.close()
    assert len(threads) == 1
    assert threads[0].thread_id.startswith("script_")
    assert threads[0].origin == "script"
    assert len(runs) == 1
    assert runs[0].status == "succeeded"
    assert control is not None
    assert isinstance(control.payload, StartControlPayload)
    assert control.payload.runnable == "agic:echo"


def test_local_script_renders_composite_flow_progress(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source = tmp_path / "research.too"
    source.write_text(_FLOW_SOURCE, encoding="utf-8")
    layout = agents.materialize_roaming_program(source)
    harness = ExecutionHarness.create(
        tmp_path / "harness",
        source=_FLOW_SOURCE,
        responses=[
            ModelCallResult(message=Message.assistant('["one","two"]')),
        ],
    )
    setup = replace(harness.setup, layout=layout)

    class _SetupWatcher:
        def __init__(self, actual_layout, **_kwargs) -> None:
            assert actual_layout == layout

        async def refresh(self):
            return setup

    monkeypatch.setattr(script, "SetupWatcher", _SetupWatcher)
    monkeypatch.setattr(
        script,
        "prepare_agent_state",
        lambda actual_layout, **_kwargs: (
            harness.state
            if actual_layout == layout
            else pytest.fail("unexpected layout")
        ),
    )
    monkeypatch.setattr(script, "configure_logging_plan", lambda _plan: None)

    result = script.dispatch(
        [],
        [str(source), "research", "-v", "agent framework"],
        prog_name="toolang",
    )
    output = capsys.readouterr()

    try:
        assert result == 0
        assert output.out == '["one","two"]\n'
        assert "Run flow research" in output.err
        assert "> agent framework" not in output.err
        assert "[0] Expand the topic." in output.err
        assert "line 10" not in output.err
        assert "Run agic expand" not in output.err
        assert '· executed ["one","two"]' in output.err
        assert "--- run_" in output.err
        assert "list returned" in output.err
        assert "~~~" not in output.err
    finally:
        asyncio.run(harness.executor.shutdown())
        harness.store.close()


def test_script_cancellation_stops_its_owned_run(tmp_path: Path) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_SOURCE,
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("unused")),
                gate=gate,
            )
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.SCRIPT)
            handle = harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="echo",
                    primary=resolve_input_parts("wait"),
                )
            )
            waiter = asyncio.create_task(script._await_script_run(handle))
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            waiter.cancel()

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiter, timeout=2)

            record = harness.store.get_run(run_id=handle.run_id)
            assert record is not None
            assert record.status == "canceled"
            assert record.error == "script interrupted"
            controls = harness.store.list_run_controls(run_id=handle.run_id)
            assert [(item.kind, item.status) for item in controls] == [
                ("start", "applied"),
                ("stop", "applied"),
            ]

    asyncio.run(scenario())
