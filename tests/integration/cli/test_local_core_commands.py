from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from collections.abc import Mapping
import sqlite3
import sys
from typing import Any, cast

import click
from click.utils import strip_ansi
from jsonschema import Draft202012Validator
import pytest
from click.testing import CliRunner

from toolang.base.types.message import Message, TextPart
from toolang.base.types.model import ModelInfo, Provider, ResolvedProvider
from toolang.base.types.policy import RunBindings
from toolang.base.types.run import ModelCall, ModelCallResult, ModelUsage, ToolCall
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.catalog import templates
from toolang.catalog.agent import LocalAgents
from toolang.catalog.job import AuthoredJobs, JobFile
import toolang.cli.toolang.commands.agent as agent_commands
import toolang.cli.toolang.commands.plugin as plugin_commands
import toolang.cli.toolang.commands.thread as thread_commands
import toolang.cli.toolang.main as cli
from toolang.cli.common.output import shorten_home_path
from toolang.common.layout import AgentLayout
from toolang.execution.history import RunHistory
from toolang.execution.records import (
    RerunControlPayload,
    RetryControlPayload,
    SteerControlPayload,
)
from toolang.execution.store import RunStore
from toolang.execution.types import Local, StepPath, ThreadPrefix
from toolang.lang.input import resolve_input_parts
from toolang.setup import AgentSetup
from toolang.plugin.models.adapters.chat_completions import (
    ChatCompletionsModelAdapter,
)
from toolang.up import process as agents
from toolang.work.state import load_ready_jobs
from toolang.work.store import JobStore
from tests.support.execution_fixtures import (
    project_run_end,
    project_run_start,
    project_step,
)
from tests.support.execution_harness import ExecutionHarness


runner = CliRunner()


@pytest.mark.parametrize("value", ("2.3", "run_root.2.3"))
def test_retry_anchor_accepts_only_dot_separated_step_paths(value: str) -> None:
    assert thread_commands._retry_anchor("run_root", value) == StepPath(
        "run_root", (2, 3)
    )

    with pytest.raises(ValueError, match="invalid step path"):
        thread_commands._retry_anchor("run_root", "run_root/2/3")


@pytest.mark.parametrize(
    "value",
    ("run_root:2.3", "run_root.01", "term_root.0", "run_root/~2"),
)
def test_inspect_rejects_invalid_pointers(value: str) -> None:
    with pytest.raises(click.ClickException, match="invalid Pointer"):
        thread_commands.parse_inspect_target(value)


def test_inspect_accepts_a_field_ref_after_a_record_ref() -> None:
    pointer = thread_commands.parse_inspect_target("run_root/output/value/0")

    assert pointer.record_ref == "run_root"
    assert pointer.field_tokens == ("output", "value", "0")


def test_inspect_without_a_target_shows_pointer_and_schema_help(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)

    result = _invoke(root, "alice", "inspect")

    assert result.exit_code == 0
    assert "A Pointer is a RECORD_REF" in result.stdout
    assert "too records" in result.stdout


@pytest.mark.parametrize(
    "arguments",
    (
        ("model_call@run_test.0",),
        ("run_test.0", "--request"),
        ("run_test.0", "--send"),
    ),
)
def test_inspect_rejects_removed_model_call_and_request_syntax(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)

    result = _invoke(root, "alice", "inspect", *arguments)

    assert result.exit_code == 2


def test_read_only_thread_commands_do_not_create_execution_store(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    layout = AgentLayout.resident(root, "alice")

    result = _invoke(root, "alice", "threads")

    assert result.exit_code == 0
    assert not layout.run_store.exists()


@pytest.mark.parametrize("schema_version", [11, 18, 24, 27, 28])
def test_read_only_thread_commands_do_not_migrate_incompatible_history(
    tmp_path: Path,
    schema_version: int,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    layout = AgentLayout.resident(root, "alice")
    layout.run_store.parent.mkdir(parents=True)
    connection = sqlite3.connect(layout.run_store)
    connection.execute("CREATE TABLE legacy_state (value TEXT NOT NULL)")
    connection.execute("INSERT INTO legacy_state VALUES ('preserved')")
    connection.execute(f"PRAGMA user_version={schema_version}")
    connection.commit()
    connection.close()

    result = _invoke(root, "alice", "threads")
    error_output = " ".join(click.unstyle(result.stderr).replace("│", " ").split())

    assert result.exit_code == 1
    assert "Traceback" not in error_output
    assert "execution history is incompatible with toolang" in error_output
    assert f"uses schema {schema_version}" in error_output
    assert "requires schema 29" in error_output
    assert "backup" in error_output
    assert "database was not changed" in error_output.lower()
    connection = sqlite3.connect(layout.run_store)
    try:
        assert (
            int(connection.execute("PRAGMA user_version").fetchone()[0])
            == schema_version
        )
        assert connection.execute("SELECT value FROM legacy_state").fetchone() == (
            "preserved",
        )
    finally:
        connection.close()


def test_thread_and_run_lists_read_local_history(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
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
            status="succeeded",
            input=(),
            output=(TextPart(text="The repository looks good."),),
            started_at="2026-07-25T01:00:00Z",
            finished_at="2026-07-25T01:00:01Z",
        )
        project_run_end(store, run_id=run.id)
    finally:
        store.close()

    threads = _invoke(root, "alice", "threads")
    runs = _invoke(
        root,
        "alice",
        "runs",
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


def test_chore_list_shows_scheduler_and_latest_run_state(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    layout = AgentLayout.resident(root, "alice")
    AuthoredJobs(layout.home).create(
        JobFile.parse(
            """---
id: maintain
title: Maintain knowledge
schedule: FREQ=HOURLY
---
Maintain the knowledge base.
""",
            kind="chore",
        )
    )
    (job,) = load_ready_jobs(layout)
    jobs = JobStore(layout.job_store)
    try:
        jobs.reconcile(jobs=(job,), now=datetime.now(timezone.utc))
    finally:
        jobs.close()
    runs = RunStore(layout.run_store)
    try:
        project_run_start(
            runs,
            run_id="run_failed",
            thread_id="chore_maintain",
            origin="chore",
            input=Message.user("Maintain the knowledge base."),
        )
        project_run_end(
            runs,
            run_id="run_failed",
            status="failed",
            error="model credits exhausted",
        )
    finally:
        runs.close()

    result = _invoke(root, "alice", "chore", "list")

    assert result.exit_code == 0, result.stderr
    assert "STATUS" in result.stdout
    assert "LAST RUN" in result.stdout
    assert "NEXT RUN" in result.stdout
    assert "ERROR" in result.stdout
    assert "Maintain knowledge" in result.stdout
    assert "pending" in result.stdout
    assert "failed" in result.stdout
    assert "model credits exhausted" in result.stdout


def test_inspect_reads_typed_run_schema_and_step_path(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
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
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart(text="prepared"),),
            started_at="2026-07-25T01:00:00Z",
            finished_at="2026-07-25T01:00:01Z",
        )
        project_run_end(store, run_id=run.id)
    finally:
        store.close()

    result = _invoke(root, "alice", "inspect", "run_inspect.0", "--json")

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["path"] == "run_inspect.0"
    assert document["kind"] == "value"
    assert document["output"] == {
        "type": "Part[]",
        "value": [{"text": "prepared", "type": "text"}],
        "name": "_",
        "dim": 0,
    }


def test_records_filters_and_expands_record_owned_variants(tmp_path: Path) -> None:
    all_records = _invoke(tmp_path, "records")
    selected = _invoke(
        tmp_path,
        "records",
        "--filter",
        "control",
        "--filter",
        "step",
    )
    control = _invoke(tmp_path, "records", "--filter", "control")
    positional = _invoke(tmp_path, "records", "thread")

    assert all_records.exit_code == 0
    assert all(
        name in all_records.stdout
        for name in ("ThreadRecord", "ControlRecord", "RunRecord", "StepRecord")
    )
    assert selected.exit_code == 0
    assert "ControlRecord" in selected.stdout
    assert "StepRecord" in selected.stdout
    assert "ThreadRecord" not in selected.stdout
    assert control.exit_code == 0
    assert "/payload (start)" in control.stdout
    assert "StartControlPayload" in control.stdout
    assert positional.exit_code == 2


def test_record_schemas_validate_exact_inspect_json(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    store = RunStore(AgentLayout.resident(root, "alice").run_store)
    try:
        run = project_run_start(
            store,
            run_id="run_schema",
            thread_id="term_schema",
            origin="chat",
            input=Message.user("Validate records"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("validated"),),
            started_at="2026-07-25T01:00:00Z",
            finished_at="2026-07-25T01:00:01Z",
        )
        project_run_end(store, run_id=run.id)
    finally:
        store.close()

    targets = {
        "thread": "term_schema",
        "control": "run_schema^0",
        "run": "run_schema",
        "step": "run_schema.0",
    }
    for kind, target in targets.items():
        inspected = _invoke(root, "alice", "inspect", target, "--json")
        discovered = _invoke(root, "records", "--filter", kind, "--json")

        assert inspected.exit_code == 0, inspected.stderr
        assert discovered.exit_code == 0, discovered.stderr
        schema = json.loads(discovered.stdout)
        document = json.loads(inspected.stdout)
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)

    field = _invoke(root, "alice", "inspect", "run_schema/status", "--json")
    assert field.exit_code == 0
    assert json.loads(field.stdout) == "succeeded"


def test_inspect_focuses_historical_model_and_tool_calls(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    store = RunStore(AgentLayout.resident(root, "alice").run_store)
    model_call = ModelCall(
        instructions="<policy>Keep XML verbatim.</policy>\nPlain instructions.",
        messages=[Message.user("<request>Review this.</request>\nPlain input.")],
    )
    tool_call = ToolCall(
        tool_call_id="tool_1",
        call_id="call_1",
        name="search",
        input={"query": "record pointers"},
    )
    try:
        run = project_run_start(
            store,
            run_id="run_focus",
            thread_id="term_focus",
            origin="chat",
            input=Message.user("Inspect calls"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="model",
            status="succeeded",
            input=(),
            output=(TextPart("model result"),),
            started_at="2026-07-25T01:00:00Z",
            finished_at="2026-07-25T01:00:01Z",
            context={"model": "test/model", "call": model_call},
        )
        project_step(
            store,
            run_id=run.id,
            step_index=1,
            kind="tool",
            status="succeeded",
            input=(),
            output=Local.typed("Json", {"matches": 1}, "_"),
            started_at="2026-07-25T01:00:01Z",
            finished_at="2026-07-25T01:00:02Z",
            context={"plugin": "search", "call": tool_call},
        )
        project_run_end(store, run_id=run.id)
    finally:
        store.close()

    model_json = _invoke(
        root,
        "alice",
        "inspect",
        "run_focus.0",
        "--focus",
        "model_call",
        "--json",
    )
    model_human = _invoke(
        root,
        "alice",
        "inspect",
        "run_focus.0",
        "--focus",
        "model_call",
    )
    tool_json = _invoke(
        root,
        "alice",
        "inspect",
        "run_focus.1",
        "--focus",
        "tool_call",
        "--json",
    )
    step_schema_result = _invoke(root, "records", "--filter", "step", "--json")
    model_record = _invoke(root, "alice", "inspect", "run_focus.0", "--json")
    tool_record = _invoke(root, "alice", "inspect", "run_focus.1", "--json")
    field_focus = _invoke(
        root,
        "alice",
        "inspect",
        "run_focus.0/given",
        "--focus",
        "model_call",
    )

    assert model_json.exit_code == 0, model_json.stderr
    model_document = json.loads(model_json.stdout)
    assert model_document["instructions"] == model_call.instructions
    assert model_document["messages"][0]["parts"][0]["text"] == (
        "<request>Review this.</request>\nPlain input."
    )
    assert model_human.exit_code == 0
    assert model_call.instructions in model_human.stdout
    assert "<request>Review this.</request>\nPlain input." in model_human.stdout
    assert tool_json.exit_code == 0
    assert json.loads(tool_json.stdout) == {
        "tool_call_id": "tool_1",
        "call_id": "call_1",
        "name": "search",
        "input": {"query": "record pointers"},
    }
    assert step_schema_result.exit_code == 0
    step_validator = Draft202012Validator(json.loads(step_schema_result.stdout))
    step_validator.validate(json.loads(model_record.stdout))
    step_validator.validate(json.loads(tool_record.stdout))
    assert field_focus.exit_code == 2
    assert "--focus requires a complete record Pointer" in field_focus.stderr


def test_inspect_projects_a_historical_provider_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    layout = AgentLayout.resident(root, "alice")
    store = RunStore(layout.run_store)
    try:
        run = project_run_start(
            store,
            run_id="run_request",
            thread_id="term_request",
            origin="chat",
            input=Message.user("Inspect request"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="model",
            status="succeeded",
            input=(),
            output=(TextPart("done"),),
            started_at="2026-07-25T01:00:00Z",
            finished_at="2026-07-25T01:00:01Z",
            context={
                "model": "test/model-v1",
                "call": ModelCall(
                    instructions="Provider instructions",
                    messages=[Message.user("Provider input")],
                ),
            },
        )
        project_run_end(store, run_id=run.id)
    finally:
        store.close()

    class _SetupSnapshot:
        def __init__(
            self,
            _layout: AgentLayout,
            *,
            binding_overrides: Mapping[str, str] | None = None,
            **_kwargs: object,
        ) -> None:
            self.bindings = RunBindings(**dict(binding_overrides or {}))

        async def refresh(self) -> AgentSetup:
            return _request_inspection_setup(layout, bindings=self.bindings)

    monkeypatch.setattr(thread_commands, "SetupWatcher", _SetupSnapshot)

    result = _invoke(
        root,
        "alice",
        "inspect",
        "run_request.0",
        "--focus",
        "model_request",
        "--default",
        "model=test/model-v1",
        "--json",
    )

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["model"] == "model-v1"
    assert document["messages"] == [
        {"role": "system", "content": "Provider instructions"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "Provider input"}],
        },
    ]
    assert document["stream"] is False


def test_inspect_previews_future_model_call_and_request_without_a_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    source = """
instruct review:
  <policy>Preserve the authored XML.</policy>

context review:
  <workspace>future-state</workspace>

agic review(_: Text) -> Text:
  recall = none
  instruct: review
  context: review
  user: <request>{{_}}</request>
"""
    harness = ExecutionHarness.create(root, source=source, responses=[])
    _create_agent(root)
    (harness.setup.layout.home / "agent.too").write_text(source, encoding="utf-8")
    layout = harness.setup.layout

    class _SetupSnapshot:
        def __init__(
            self,
            _layout: AgentLayout,
            *,
            binding_overrides: Mapping[str, str] | None = None,
            **_kwargs: object,
        ) -> None:
            self.bindings = RunBindings(**dict(binding_overrides or {}))

        async def refresh(self) -> AgentSetup:
            return _request_inspection_setup(layout, bindings=self.bindings)

    class _StateSnapshot:
        def __init__(self, _layout: AgentLayout) -> None:
            pass

        async def refresh(self):
            return harness.state

    monkeypatch.setattr(thread_commands, "SetupWatcher", _SetupSnapshot)
    monkeypatch.setattr(thread_commands, "StateWatcher", _StateSnapshot)

    model_call = _invoke(
        root,
        "alice",
        "inspect",
        "--focus",
        "model_call",
        "--default",
        "runnable=agic:review",
        "--input",
        "Review me",
        "--json",
    )
    model_request = _invoke(
        root,
        "alice",
        "inspect",
        "--focus",
        "model_request",
        "--default",
        "runnable=agic:review",
        "--default",
        "model=test/model-v1",
        "--input",
        "Review me",
        "--json",
    )

    assert model_call.exit_code == 0, model_call.stderr
    call_document = json.loads(model_call.stdout)
    assert (
        "<policy>Preserve the authored XML.</policy>" in call_document["instructions"]
    )
    prospective_text = call_document["messages"][-1]["parts"][0]["text"]
    assert "<workspace>future-state</workspace>" in prospective_text
    assert prospective_text.endswith("<request>Review me</request>")
    assert model_request.exit_code == 0, model_request.stderr
    request_document = json.loads(model_request.stdout)
    assert request_document["model"] == "model-v1"
    assert request_document["messages"][-1]["role"] == "user"
    request_text = request_document["messages"][-1]["content"][0]["text"]
    assert "<workspace>future-state</workspace>" in request_text
    assert request_text.endswith("<request>Review me</request>")
    assert harness.store.list_runs(thread_id=None) == []
    harness.store.close()


def test_inspect_step_path_does_not_cross_run_boundaries(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    store = RunStore(AgentLayout.resident(root, "alice").run_store)
    try:
        parent = project_run_start(
            store,
            run_id="run_parent",
            thread_id="term_step_owner",
            origin="chat",
            input=Message.user("Inspect parent"),
        )
        parent_step = project_step(
            store,
            run_id=parent.id,
            step_index=0,
            kind="run",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-07-25T01:00:00Z",
            finished_at="2026-07-25T01:00:01Z",
        )
        child = project_run_start(
            store,
            run_id="run_child",
            thread_id=parent.thread,
            origin="chat",
            input=Message.user("Inspect child"),
            parent=parent_step.path,
        )
        project_step(
            store,
            run_id=child.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart(text="child"),),
            started_at="2026-07-25T01:00:01Z",
            finished_at="2026-07-25T01:00:02Z",
        )
        project_run_end(store, run_id=child.id)
        project_run_end(store, run_id=parent.id)
    finally:
        store.close()

    child_result = _invoke(root, "alice", "inspect", "run_child.0", "--json")
    synthetic_result = _invoke(root, "alice", "inspect", "run_parent.0.0", "--json")

    assert child_result.exit_code == 0
    assert json.loads(child_result.stdout)["path"] == "run_child.0"
    assert synthetic_result.exit_code == 1
    assert "record not found: run_parent.0.0" in synthetic_result.stderr


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
            kind="value",
            status="succeeded",
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
    inspect = cli.main([str(source), "inspect", "run_roaming.0", "--json"])
    inspect_output = capsys.readouterr()

    assert threads == 0
    assert "script_roaming" in threads_output.out
    assert "Inspect roaming history" in threads_output.out
    assert runs == 0
    assert "run_roaming" in runs_output.out
    assert inspect == 0
    document = json.loads(inspect_output.out)
    assert document["path"] == "run_roaming.0"
    assert document["kind"] == "value"
    assert document["output"] == {
        "type": "Part[]",
        "value": [{"text": "ready", "type": "text"}],
        "name": "_",
        "dim": 0,
    }


def test_visiting_selector_reads_inspection_without_fetching(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    selector = "brice/researcher"
    layout = AgentLayout(
        root=tmp_path / "visiting",
        name="researcher",
        placement="visiting",
    )
    store = RunStore(layout.run_store)
    try:
        run = project_run_start(
            store,
            run_id="run_visiting",
            thread_id="term_visiting",
            origin="chat",
            input=Message.user("Inspect visiting history"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart(text="cached"),),
            started_at="2026-07-25T01:00:00Z",
            finished_at="2026-07-25T01:00:01Z",
        )
        project_run_end(store, run_id=run.id)
    finally:
        store.close()

    def unexpected_fetch(*_args: object, **_kwargs: object) -> AgentLayout:
        raise AssertionError("inspect must not fetch the visiting source")

    monkeypatch.setattr(agents, "visiting_layout", lambda _selector: layout)
    monkeypatch.setattr(agents, "resolve_visiting_layout", unexpected_fetch)

    result = cli.main([selector, "inspect", "run_visiting.0", "--json"])
    output = capsys.readouterr()

    assert result == 0
    document = json.loads(output.out)
    assert document["path"] == "run_visiting.0"
    assert document["kind"] == "value"
    assert document["output"] == {
        "type": "Part[]",
        "value": [{"text": "cached", "type": "text"}],
        "name": "_",
        "dim": 0,
    }


def test_run_controls_are_persisted_without_an_api_server(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
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

    steer = _invoke(root, "alice", "steer", "term_active", "Focus on tests")
    cancel = _invoke(root, "alice", "cancel", "run_active")

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
        ("start", "immediate", "applied"),
        ("steer", "next_step", "pending"),
        ("stop", "immediate", "pending"),
    ]
    assert isinstance(controls[1].payload, SteerControlPayload)
    assert controls[1].payload.locals == (
        Local.typed("Part[]", Message.user("Focus on tests").parts, "_", 0),
    )


@pytest.mark.parametrize("tty", (False, True), ids=("non-tty", "tty"))
def test_retry_and_rerun_execute_locally_with_limit_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tty: bool,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        source="""
agic reply(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            RuntimeError("temporary failure"),
            ModelCallResult(
                message=Message.assistant("recovered"),
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
            ModelCallResult(message=Message.assistant("reran")),
        ],
    )
    _create_agent(harness.setup.layout.root)

    class _SetupSnapshot:
        def __init__(
            self,
            _layout: AgentLayout,
            *,
            limit_overrides: Mapping[str, object] | None = None,
            **_kwargs: object,
        ) -> None:
            self.setup = replace(
                harness.setup,
                limits=replace(
                    harness.setup.limits,
                    **dict(limit_overrides or {}),
                ),
            )

        async def refresh(self) -> AgentSetup:
            return self.setup

    class _StateSnapshot:
        def __init__(self, _layout: AgentLayout) -> None:
            pass

        async def refresh(self):
            return harness.state

    monkeypatch.setattr(thread_commands, "SetupWatcher", _SetupSnapshot)
    monkeypatch.setattr(thread_commands, "StateWatcher", _StateSnapshot)

    async def start_source():
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
        source = await harness.executor.start(
            harness.run_spec(
                thread=thread,
                runnable="reply",
                primary=resolve_input_parts("hello"),
            )
        )
        await harness.executor.shutdown()
        return source

    try:
        source = asyncio.run(start_source())
        assert source.status == "failed"

        retry = _invoke(
            harness.setup.layout.root,
            "alice",
            "retry",
            source.id,
            "--limit",
            "tokens=10",
            tty=tty,
        )
        retried = harness.store.get_run(run_id=source.id)
        assert retry.exit_code == 0, (
            retry.stdout,
            retry.stderr,
            repr(retry.exception),
            retried.status if retried is not None else None,
            retried.error if retried is not None else None,
            harness.adapter.pending_responses,
        )
        rerun = _invoke(
            harness.setup.layout.root,
            "alice",
            "rerun",
            source.id,
            "--limit",
            "time=30",
            tty=tty,
        )

        assert rerun.exit_code == 0, rerun.stderr
        rerun_records = [
            run
            for run in harness.store.list_runs(thread_id=source.thread)
            if run.id != source.id
        ]
        assert len(rerun_records) == 1
        rerun_id = rerun_records[0].id
        if tty:
            assert retry.stdout == ""
            retry_output = strip_ansi(retry.stderr)
            assert "• recovered" in retry_output
            retry_footer = next(
                line
                for line in retry_output.splitlines()
                if f"{source.id}: retry succeeded" in line
            )
            assert retry_footer.startswith(f"[ {source.id}: retry succeeded · ")

            assert rerun.stdout == ""
            rerun_output = strip_ansi(rerun.stderr)
            assert "• reran" in rerun_output
            rerun_footer = next(
                line
                for line in rerun_output.splitlines()
                if f"{rerun_id}: rerun succeeded" in line
            )
            assert rerun_footer.startswith(f"[ {rerun_id}: rerun succeeded · ")
        else:
            assert retry.stdout.strip() == f"retried {source.id}: succeeded"
            assert rerun.stdout.strip() == (
                f"reran {source.id} as {rerun_id}: succeeded"
            )
            assert retry.stderr == ""
            assert rerun.stderr == ""

        retry_control = harness.store.list_run_controls(run_id=source.id)[-1]
        rerun_control = harness.store.get_run_control(run_id=rerun_id, index=0)
        assert retry_control.kind == "retry"
        assert isinstance(retry_control.payload, RetryControlPayload)
        assert retry_control.payload.limits.tokens == 10
        assert rerun_control is not None
        assert rerun_control.kind == "rerun"
        assert isinstance(rerun_control.payload, RerunControlPayload)
        assert rerun_control.payload.rerun_from == source.id
        assert rerun_control.payload.limits.time == 30
    finally:
        harness.store.close()


def test_thread_fork_and_rewind_use_thread_manager_semantics(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
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

    fork = _invoke(root, "alice", "fork", "term_source")

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

    rewind = _invoke(root, "alice", "rewind", "term_source")

    assert rewind.exit_code == 0
    assert rewind.stdout.strip() == "rewound term_source before run_second"
    reopened = RunStore(layout.run_store)
    try:
        rewound = RunHistory(reopened).get_thread("term_source")
    finally:
        reopened.close()
    assert rewound is not None
    assert [run.id for run in rewound.runs] == ["run_first"]


def test_tools_uses_setup_snapshot(tmp_path: Path, monkeypatch) -> None:
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

    result = _invoke(root, "tools")

    assert result.exit_code == 0
    assert "shell" in result.stdout
    assert "echo" in result.stdout
    assert "Echo text." in result.stdout


def test_sandboxes_lists_installed_plugins(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang"
    monkeypatch.setattr(
        plugin_commands,
        "plugin_info_rows",
        lambda group: [("docker", "test")] if group == "toolang.sandbox" else [],
    )

    result = _invoke(root, "sandboxes")

    assert result.exit_code == 0
    assert "docker" in result.stdout
    assert "test" in result.stdout


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


def test_roaming_agent_info_uses_the_source_layout(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source = tmp_path / "demo.too"
    source.write_text(
        templates.render_template("agent", agent_name="demo", name="demo"),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}
    shortened: list[Path] = []

    def _shorten_home(path: Path) -> str:
        shortened.append(path)
        return "compact home"

    monkeypatch.setattr(agent_commands, "SetupWatcher", _EmptySetupWatcher)
    monkeypatch.setattr(
        agent_commands,
        "shorten_home_path",
        _shorten_home,
        raising=False,
    )
    monkeypatch.setattr(
        agent_commands,
        "echo_pairs_table",
        lambda rows, *, avatar, title: captured.update(
            rows=dict(rows),
            avatar=avatar,
            title=title,
        ),
    )

    result = cli.main([str(source), "info"])
    capsys.readouterr()

    assert result == 0
    assert captured["title"] == "DEMO"
    rows = cast(dict[str, str], captured["rows"])
    assert shortened == [AgentLayout.roaming(source).home]
    assert rows["Home"] == "compact home"


def test_visiting_agent_info_uses_the_materialized_layout(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    selector = "brice/researcher"
    layout = AgentLayout(
        root=tmp_path / "visiting",
        name="researcher",
        placement="visiting",
    )
    layout.home.mkdir(parents=True)
    layout.program.write_text(
        templates.render_template(
            "agent",
            agent_name="researcher",
            name="researcher",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_commands, "SetupWatcher", _EmptySetupWatcher)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        agent_commands,
        "echo_pairs_table",
        lambda rows, *, avatar, title: captured.update(
            rows=dict(rows),
            avatar=avatar,
            title=title,
        ),
    )
    monkeypatch.setattr(
        agents,
        "resolve_visiting_layout",
        lambda *_args, **_kwargs: layout,
    )

    result = cli.main(["info", selector])
    capsys.readouterr()

    assert result == 0
    assert captured["title"] == "RESEARCHER"
    rows = cast(dict[str, str], captured["rows"])
    assert rows["Home"] == shorten_home_path(layout.home)


def _invoke(root: Path, *args: str, tty: bool = False):
    @click.command(
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True}
    )
    @click.argument("arguments", nargs=-1, type=click.UNPROCESSED)
    def public_cli(arguments: tuple[str, ...]) -> None:
        if tty:
            setattr(sys.stderr, "isatty", lambda: True)
        raise click.exceptions.Exit(cli.main(["--root", str(root), *arguments]))

    return runner.invoke(public_cli, list(args), env={})


def _create_agent(root: Path, name: str = "alice") -> None:
    agents = LocalAgents(root / "agents")
    content = templates.render_template("agent", agent_name=name, name=name)
    if agents.get(name) is not None:
        return
    home = agents.path(name)
    home.mkdir(parents=True, exist_ok=True)
    (home / "agent.too").write_text(content, encoding="utf-8")


def _request_inspection_setup(
    layout: AgentLayout,
    *,
    bindings: RunBindings,
) -> AgentSetup:
    provider = Provider(
        id="test",
        name="Test",
        env=(),
        npm="@ai-sdk/openai-compatible",
        models={},
        resolved=ResolvedProvider(
            adapter="chat_completions",
            api="https://example.test/v1",
            env=(),
            ready=True,
        ),
    )
    adapter = ChatCompletionsModelAdapter()
    return AgentSetup(
        layout=layout,
        providers={provider.id: provider},
        adapters={adapter.name: adapter},
        models=(
            ModelInfo(
                ref="test/model-v1",
                provider="test",
                name="Model V1",
                model="model-v1",
                selectors=("test/model-v1",),
                adapter=adapter.name,
                streaming=False,
                metadata={"resolved_ready": True},
            ),
        ),
        tools={},
        envs={},
        bindings=bindings,
    )


class _EmptySetupWatcher:
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
