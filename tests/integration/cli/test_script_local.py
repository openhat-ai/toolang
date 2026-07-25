from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.cli.toolang.commands import script
from toolang.execution.store import RunStore
from toolang.up import process as agents
from tests.support.execution_harness import ExecutionHarness


_SOURCE = """
agic echo(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
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
        def __init__(self, actual_layout) -> None:
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
    finally:
        store.close()
        asyncio.run(harness.executor.shutdown())
        harness.store.close()
    assert len(threads) == 1
    assert threads[0].thread_id.startswith("script_")
    assert threads[0].origin == "script"
    assert len(runs) == 1
    assert runs[0].status == "finished"
    assert runs[0].runnable_name == "echo"
