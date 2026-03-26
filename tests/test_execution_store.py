from __future__ import annotations

from pathlib import Path

from toolang.agent.resolve import resolve_agent_ref
from toolang.concepts.execution import thread_group_for_origin
from toolang.runtime.execution_store import ExecutionStore


def test_execution_store_records_activation_thread_run_and_steps(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text("thunk chat(user):\n  user\n", encoding="utf-8")

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    store = ExecutionStore(home / ".toolang" / "agents" / "alice" / "execution.db")

    activation = store.begin_activation(
        agent=agent,
        activation_id="act1",
        activation_kind="invoke",
        sandbox="host",
        cap_scopes=("agent", "shared"),
    )
    thread = store.ensure_thread(
        agent=agent,
        thread_id="invoke:run1",
        thread_group=thread_group_for_origin("invoke"),
        title="hello",
    )
    run = store.start_run(
        run_id="run1",
        activation_id=activation.activation_id,
        thread_id=thread.thread_id,
        origin="invoke",
        channel=None,
        sender="self",
        execution_strategy="direct",
        input_text="hello",
    )
    prompt_step = store.append_step(
        run_id=run.run_id,
        step_kind="prompt_build",
        status="finished",
        input_json={"raw_input": "hello"},
        output_json={"message_count": 2},
    )
    model_step = store.append_step(
        run_id=run.run_id,
        step_kind="model_call",
        status="finished",
        input_json={"model": "gpt-5.3"},
        output_json={"output_length": 5},
    )
    finished_run = store.finish_run(run_id=run.run_id, output_text="world")
    finished_activation = store.finish_activation(
        activation_id=activation.activation_id,
        status="finished",
    )

    activations = store.list_activations(agent_uri=agent.uri)
    runs = store.list_runs(activation_id=activation.activation_id)
    steps = store.list_steps(run_id=run.run_id)
    store.close()

    assert activation.status == "running"
    assert thread.thread_group == "invoke"
    assert prompt_step.seq == 1
    assert model_step.seq == 2
    assert finished_run.status == "finished"
    assert finished_run.output_text == "world"
    assert finished_activation.status == "finished"
    assert [item.activation_id for item in activations] == ["act1"]
    assert [item.run_id for item in runs] == ["run1"]
    assert [item.step_kind for item in steps] == ["prompt_build", "model_call"]
