from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.cli.toolang.commands import script
from toolang.execution.store import RunStore
from toolang.execution.records import RunControlPayload
from toolang.up import process as agents
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.execution.types import ErrorMessage, ThreadPrefix
from toolang.lang.input import resolve_input_parts
from toolang.state.state import publish_state_resources
from toolang.state.watcher import StateRefresh


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


@pytest.mark.parametrize(
    ("save_mode", "expected_status", "expected_stdout"),
    (
        (None, 0, ""),
        ("stdout", 0, "done"),
        ("file", 0, ""),
        ("missing-parent", 1, ""),
    ),
)
def test_local_script_saves_only_to_an_explicit_destination(
    tmp_path: Path,
    monkeypatch,
    capsys,
    save_mode: str | None,
    expected_status: int,
    expected_stdout: str,
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
        def __init__(self, actual_layout, **kwargs) -> None:
            assert actual_layout == layout
            assert kwargs["sandbox"] == "host"

        async def refresh(self):
            return setup

    monkeypatch.setattr(script, "SetupWatcher", _SetupWatcher)
    publication = publish_state_resources(harness.state, agent_name=layout.name)

    class _StateWatcher:
        def __init__(self, actual_layout, *, initial_state, **_kwargs) -> None:
            assert actual_layout == layout
            assert initial_state is harness.state

        def current(self):
            return publication

        async def refresh(self):
            raise AssertionError("local Script must reuse its prepared initial State")

        async def refresh_result(self):
            return StateRefresh(publication)

    monkeypatch.setattr(script, "StateWatcher", _StateWatcher)
    quiet = save_mode in {"stdout", "file"}

    def prepare_state(actual_layout, **kwargs):
        assert actual_layout == layout
        assert (kwargs["progress"] is None) is quiet
        return harness.state

    monkeypatch.setattr(script, "prepare_agent_state", prepare_state)
    monkeypatch.setattr(script, "configure_logging_plan", lambda _plan: None)

    args = [str(source), "echo"]
    destination = tmp_path / "result.txt"
    if save_mode == "stdout":
        args.extend(("--quiet", "--save", "-"))
    elif save_mode == "file":
        args.extend(("--quiet", "--save", str(destination)))
    elif save_mode == "missing-parent":
        destination = tmp_path / "missing" / "result.txt"
        args.extend(("--save", str(destination)))
    args.extend(("--", "hello"))
    result = script.dispatch([], args, prog_name="toolang")
    output = capsys.readouterr()

    assert result == expected_status
    assert output.out == expected_stdout
    if save_mode == "missing-parent":
        assert "result destination parent does not exist" in output.err
        assert "∎ run_" in output.err
    elif quiet:
        assert output.err == ""
    else:
        assert "• done" in output.err
        assert "∎ run_" in output.err
        assert "\x1b[" not in output.err
    if save_mode == "file":
        assert destination.read_bytes() == b"done"
    else:
        assert not destination.exists()
    store = RunStore(layout.run_store)
    try:
        threads = store.list_threads()
        runs = store.list_runs(thread_id=threads[0].id, limit=None)
        control = store.get_run_control(run_id=runs[0].id, index=0)
        durable_output = store.run_output(run_id=runs[0].id)
    finally:
        store.close()
        asyncio.run(harness.executor.stop())
        harness.store.close()
    assert len(threads) == 1
    assert threads[0].id.startswith("script_")
    assert threads[0].origin == "script"
    assert len(runs) == 1
    assert runs[0].status == "succeeded"
    assert durable_output == (TextPart("done"),)
    assert control is not None
    assert isinstance(control.payload, RunControlPayload)
    assert control.payload.runnable == "agent$agic:echo"


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
        [str(source), "research", "--", "agent framework"],
        prog_name="toolang",
    )
    output = capsys.readouterr()

    try:
        assert result == 0
        assert output.out == ""
        assert "Run flow research" not in output.err
        assert "> agent framework" not in output.err
        assert "[0] Expand the topic." in output.err
        assert "line 10" not in output.err
        assert "Run agic expand" not in output.err
        assert '• ["one","two"]' in output.err
        assert "∎ run_" in output.err
        assert "list returned" not in output.err
        assert "~~~" not in output.err
        assert "\x1b[" not in output.err
    finally:
        asyncio.run(harness.executor.stop())
        harness.store.close()


def test_script_cancellation_cancels_its_owned_run(tmp_path: Path) -> None:
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
            handle = harness.executor.run(
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
            assert record.error == ErrorMessage("script interrupted")
            controls = harness.store.list_run_controls(run_id=handle.run_id)
            assert [(item.kind, item.status) for item in controls] == [
                ("run", "applied"),
                ("cancel", "applied"),
            ]

    asyncio.run(scenario())
