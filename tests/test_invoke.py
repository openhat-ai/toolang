from __future__ import annotations

from pathlib import Path

from toolang.agent_refs import resolve_agent_ref
from toolang.bus.db import BusStore
from toolang.invoke import invoke_prepared_agent
from toolang.layout import bus_events_db_path, resolve_toolang_root
from toolang.prepared import prepare_agent

SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"


def test_invoke_prepared_agent_records_run_events(tmp_path: Path, monkeypatch) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(
        "toolang.invoke.execute_thunk",
        lambda program, thunk, program_path, *, user_input, model=None: (
            f"ran:{thunk.name}:{user_input}:{model}"
        ),
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)
    thunk = prepared.program.get_thunk("summarize")

    result = invoke_prepared_agent(
        prepared,
        thunk,
        bus_db_path=bus_events_db_path(root),
        user_input="hello",
        model="gpt-5.3",
    )

    store = BusStore(bus_events_db_path(root))
    runs = store.list_runs(agent_uri=agent.agent_uri)
    events = store.list_events(agent_uri=agent.agent_uri)
    store.close()

    assert result.output == "ran:summarize:hello:gpt-5.3"
    assert len(result.run_id) == 32
    assert [run.status for run in runs] == ["finished"]
    assert runs[0].summary == "alice:summarize"
    assert [event.event_type for event in events] == ["run_started", "run_finished"]
