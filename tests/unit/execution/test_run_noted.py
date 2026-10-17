"""Final Run values round-trip inside the existing terminal transaction."""

from dataclasses import replace
from contextlib import closing

import pytest

from tests.support.execution_fixtures import project_run_start
from toolang.base.types.message import Message
from toolang.execution.records import run_noted_from_data, run_noted_to_data
from toolang.execution.schemas import record_to_data
from toolang.execution.store import RunStore
from toolang.execution.types import CallCounters, RunNoted, TokenUsage


def test_run_noted_roundtrip_and_conflicting_terminal_values(tmp_path):
    path = tmp_path / "runs.db"
    store = RunStore(path)
    run = project_run_start(
        store,
        run_id="run_totals",
        thread_id="chat_test",
        origin="chat",
        input=Message.user("test"),
    )
    assert run.noted is None
    noted = RunNoted(CallCounters(2, 1), TokenUsage(15, 8, 2, 3), 0.25, True)
    finished = "2026-10-17T00:00:00Z"
    terminal = store.finish_run(run_id=run.id, noted=noted, finished_at=finished)
    assert terminal.noted == noted
    assert (
        store.finish_run(run_id=run.id, noted=noted, finished_at=finished) == terminal
    )
    with pytest.raises(ValueError, match="conflicting run_end"):
        store.finish_run(
            run_id=run.id,
            noted=replace(noted, calls=CallCounters(3, 1)),
            finished_at=finished,
        )
    store.close()
    with closing(RunStore(path, read_only=True)) as reopened:
        saved = reopened.get_run(run_id=run.id)
        assert saved is not None and saved == terminal
        data = record_to_data(saved)
        assert data["noted"] == run_noted_to_data(noted)
    assert run_noted_from_data(run_noted_to_data(noted)) == noted
