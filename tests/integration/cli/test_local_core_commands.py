from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from typer.testing import CliRunner

from toolang.base.types.message import Message, TextPart
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.catalog import templates
from toolang.catalog.agent import LocalAgents
import toolang.cli.toolang.commands.agent as agent_commands
import toolang.cli.toolang.commands.plugin as plugin_commands
import toolang.cli.toolang.main as cli
from toolang.common.layout import AgentLayout
from toolang.execution.history import RunHistory
from toolang.execution.store import RunStore
from toolang.setup import AgentSetup
from toolang.up import process as agents
from tests.support.execution_fixtures import (
    project_run_end,
    project_run_start,
    project_step,
)


runner = CliRunner()


def test_read_only_thread_commands_do_not_create_execution_store(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    layout = AgentLayout.resident(root, "alice")

    result = _invoke(root, "threads", "alice")

    assert result.exit_code == 0
    assert not layout.run_store.exists()


def test_thread_and_run_lists_read_local_history(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    layout = AgentLayout.resident(root, "alice")
    store = RunStore(layout.run_store)
    try:
        run = project_run_start(
            store,
            run_id="run_first",
            thread_id="term_main",
            origin="chat",
            input=Message.user("Review the repository"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="model",
            status="finished",
            input=(),
            output=(TextPart(text="The repository looks good."),),
            started_at="2026-07-25T01:00:00Z",
            finished_at="2026-07-25T01:00:01Z",
        )
        project_run_end(store, run_id=run.id)
    finally:
        store.close()

    threads = _invoke(root, "threads", "alice")
    runs = _invoke(
        root,
        "runs",
        "alice",
        "--thread",
        "term_main",
        "--status",
        "succeeded",
    )

    assert threads.exit_code == 0
    assert "term_main" in threads.stdout
    assert "Review the repository" in threads.stdout
    assert runs.exit_code == 0
    assert "run_first" in runs.stdout
    assert "The repository looks good." in runs.stdout
    assert "succeeded" in runs.stdout


def test_inspect_reads_typed_run_schema_and_step_path(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    store = RunStore(AgentLayout.resident(root, "alice").run_store)
    try:
        run = project_run_start(
            store,
            run_id="run_inspect",
            thread_id="term_inspect",
            origin="chat",
            input=Message.user("Inspect this"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="system",
            status="finished",
            input=(),
            output=(TextPart(text="prepared"),),
            started_at="2026-07-25T01:00:00Z",
            finished_at="2026-07-25T01:00:01Z",
        )
        project_run_end(store, run_id=run.id)
    finally:
        store.close()

    result = _invoke(root, "inspect", "alice", "run_inspect:0", "--json")

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["kind"] == "step"
    assert document["run"]["id"] == "run_inspect"
    assert document["step"]["path"] == "0"
    assert document["step"]["kind"] == "system"
    assert document["step"]["output"] == [{"text": "prepared", "type": "text"}]


def test_roaming_source_reads_threads_runs_and_inspection(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic demo:\n  Reply directly.\n", encoding="utf-8")
    layout = agents.materialize_roaming_program(source)
    store = RunStore(layout.run_store)
    try:
        run = project_run_start(
            store,
            run_id="run_roaming",
            thread_id="script_roaming",
            origin="script",
            input=Message.user("Inspect roaming history"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="system",
            status="finished",
            input=(),
            output=(TextPart(text="ready"),),
            started_at="2026-07-25T01:00:00Z",
            finished_at="2026-07-25T01:00:01Z",
        )
        project_run_end(store, run_id=run.id)
    finally:
        store.close()

    threads = cli.main([str(source), "threads"])
    threads_output = capsys.readouterr()
    runs = cli.main([str(source), "runs", "--thread", "script_roaming"])
    runs_output = capsys.readouterr()
    inspect = cli.main([str(source), "inspect", "run_roaming:0", "--json"])
    inspect_output = capsys.readouterr()

    assert threads == 0
    assert "script_roaming" in threads_output.out
    assert "Inspect roaming history" in threads_output.out
    assert runs == 0
    assert "run_roaming" in runs_output.out
    assert inspect == 0
    document = json.loads(inspect_output.out)
    assert document["kind"] == "step"
    assert document["run"]["id"] == "run_roaming"
    assert document["step"]["output"] == [{"text": "ready", "type": "text"}]


def test_run_controls_are_persisted_without_an_api_server(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    layout = AgentLayout.resident(root, "alice")
    store = RunStore(layout.run_store)
    try:
        project_run_start(
            store,
            run_id="run_active",
            thread_id="term_active",
            origin="chat",
            input=Message.user("Start"),
        )
    finally:
        store.close()

    steer = _invoke(root, "steer", "alice", "term_active", "Focus on tests")
    cancel = _invoke(root, "cancel", "alice", "run_active")

    assert steer.exit_code == 0
    assert steer.stdout.strip() == "steered run_active"
    assert cancel.exit_code == 0
    assert cancel.stdout.strip() == "canceled run_active"
    reopened = RunStore(layout.run_store)
    try:
        controls = reopened.list_run_controls(run_id="run_active")
    finally:
        reopened.close()
    assert [(item.kind, item.timing, item.status) for item in controls] == [
        ("start", "immediate", "finished"),
        ("steer", "next_step", "pending"),
        ("stop", "immediate", "pending"),
    ]
    assert controls[1].input == Message.user("Focus on tests")


def test_thread_fork_and_rewind_use_thread_manager_semantics(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    layout = AgentLayout.resident(root, "alice")
    store = RunStore(layout.run_store)
    try:
        first = project_run_start(
            store,
            run_id="run_first",
            thread_id="term_source",
            origin="chat",
            input=Message.user("First"),
        )
        project_run_end(store, run_id=first.id)
        second = project_run_start(
            store,
            run_id="run_second",
            thread_id="term_source",
            origin="chat",
            input=Message.user("Second"),
        )
        project_run_end(store, run_id=second.id)
    finally:
        store.close()

    fork = _invoke(root, "fork", "alice", "term_source")

    assert fork.exit_code == 0
    words = fork.stdout.strip().split()
    assert words[0] == "forked"
    forked_id = words[1]
    assert words[2:] == ["through", "run_second"]
    reopened = RunStore(layout.run_store)
    try:
        forked = RunHistory(reopened).get_thread(forked_id)
    finally:
        reopened.close()
    assert forked is not None
    assert [run.id for run in forked.runs] == ["run_first", "run_second"]

    rewind = _invoke(root, "rewind", "alice", "term_source")

    assert rewind.exit_code == 0
    assert rewind.stdout.strip() == "rewound term_source before run_second"
    reopened = RunStore(layout.run_store)
    try:
        rewound = RunHistory(reopened).get_thread("term_source")
    finally:
        reopened.close()
    assert rewound is not None
    assert [run.id for run in rewound.runs] == ["run_first"]


def test_tool_list_uses_setup_snapshot(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang"
    layout = AgentLayout.resident(root, "default")
    tool = _FakeTool()
    setup = AgentSetup(
        layout=layout,
        providers={},
        adapters={},
        models=(),
        tools={"shell__echo": tool},
        envs={},
    )
    monkeypatch.setattr(
        plugin_commands,
        "_setup",
        lambda _layout, *, force=False: setup,
    )
    monkeypatch.setattr(
        plugin_commands,
        "plugin_sources",
        lambda _group: {"shell": "test"},
    )

    result = _invoke(root, "tool", "list")

    assert result.exit_code == 0
    assert "shell" in result.stdout
    assert "echo" in result.stdout
    assert "Echo text." in result.stdout


def test_agent_info_builds_state_and_setup_without_server(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "toolang"
    LocalAgents(root / "agents").create(
        "alice",
        content=templates.render_template("agent", agent_name="alice", name="alice"),
    )

    class _SetupWatcher:
        def __init__(self, layout: AgentLayout) -> None:
            self.layout = layout

        async def refresh(self) -> AgentSetup:
            return AgentSetup(
                layout=self.layout,
                providers={},
                adapters={},
                models=(),
                tools={},
                envs={},
            )

    monkeypatch.setattr(agent_commands, "SetupWatcher", _SetupWatcher)

    result = _invoke(root, "info", "alice")

    assert result.exit_code == 0
    assert "ALICE" in result.stdout
    assert "Caps" in result.stdout
    assert "Models" in result.stdout
    assert "Tools" in result.stdout
    assert "stopped" in result.stdout


def _invoke(root: Path, *args: str):
    return runner.invoke(cli.app, ["--root", str(root), *args], env={})


class _FakeTool:
    name = "echo"
    plugin_name = "shell"
    namespace = "shell"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description="Echo text.")

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del context
        return dict(arguments)
