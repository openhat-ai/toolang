from __future__ import annotations

from pathlib import Path

from toolang.agent.prepared import prepare_agent
from toolang.agent.resolve import resolve_agent_ref
from toolang.bus.db import BusStore
from toolang.concepts.layout import AgentHome, ToolangRoot
from toolang.concepts.persisted.prompt_trace import PromptTrace
from toolang.runtime.invoke import invoke_prepared_agent


def resolve_toolang_root(root: Path) -> Path:
    return ToolangRoot.resolve(root).path


def bus_events_db_path(root: Path) -> Path:
    return ToolangRoot.resolve(root).bus_events_db_path


def agent_run_prompt_path(agent_home: Path, agent_name: str, run_id: str) -> Path:
    return AgentHome.resolve(agent_home).room(agent_name).prompt_trace_path(run_id)

SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"


def test_invoke_prepared_agent_records_run_events(tmp_path: Path, monkeypatch) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build",
        lambda build: (
            f"ran:{build.runtime_context['program']['thunk']['name']}:{build.raw_input}:{build.model}"
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
    runs = store.list_runs(agent_uri=agent.uri)
    events = store.list_events(agent_uri=agent.uri)
    store.close()
    trace = PromptTrace.load(agent_run_prompt_path(home, "alice", result.run_id))

    assert result.output == "ran:summarize:hello:gpt-5.3"
    assert len(result.run_id) == 32
    assert [run.status for run in runs] == ["finished"]
    assert runs[0].summary == "alice:summarize"
    assert [event.event_type for event in events] == ["run_started", "run_finished"]
    assert trace.sandbox == "host"
    assert trace.cap_scopes == ["agent", "shared", "global"]
    assert trace.runtime_context["program"]["thunk"]["name"] == "summarize"
    assert trace.runtime_context["visible_caps"]["services"][0]["name"] == "github"
    assert trace.response_text == "ran:summarize:hello:gpt-5.3"
