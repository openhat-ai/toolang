from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, cast

import click
from click.utils import strip_ansi
import pytest
from click.testing import CliRunner

from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCall, ModelCallResult, ModelUsage
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
from toolang.execution.events import StepBegin
from toolang.execution.records import (
    RerunControlPayload,
    RetryControlPayload,
    SteerControlPayload,
)
from toolang.execution.store import RunStore
from toolang.execution.types import Local, ModelStepGiven, StepPath, ThreadPrefix
from toolang.lang.input import resolve_input_parts
from toolang.setup import AgentSetup
from toolang.up import process as agents
from toolang.work.state import load_ready_jobs
from toolang.work.store import JobStore
from tests.support.execution_fixtures import (
    persist_event,
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
    ("run_root:2.3", "run_root/2/3", "run_root.01", "term_root.0"),
)
def test_inspect_rejects_noncanonical_step_paths(value: str) -> None:
    with pytest.raises(click.ClickException, match="invalid inspect path"):
        thread_commands.parse_inspect_target(value)


def test_read_only_thread_commands_do_not_create_execution_store(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    layout = AgentLayout.resident(root, "alice")

    result = _invoke(root, "alice", "threads")

    assert result.exit_code == 0
    assert not layout.run_store.exists()


@pytest.mark.parametrize("schema_version", [11, 18, 24, 27])
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
    assert "requires schema 28" in error_output
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
    assert document["kind"] == "step"
    assert document["target"] == "run_inspect.0"
    assert document["run"]["id"] == "run_inspect"
    assert document["step"]["path"] == "run_inspect.0"
    assert document["step"]["kind"] == "value"
    assert document["step"]["output"] == {
        "type": "Part[]",
        "value": [{"text": "prepared", "type": "text"}],
        "name": "_",
        "dim": 0,
    }

    model_call_result = _invoke(
        root,
        "alice",
        "inspect",
        "model_call@run_inspect.0",
    )
    assert model_call_result.exit_code == 1
    assert "step is not a model call: run_inspect.0" in model_call_result.stderr


def test_inspect_reads_historical_model_call_as_a_direct_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    store = RunStore(AgentLayout.resident(root, "alice").run_store)
    try:
        run = project_run_start(
            store,
            run_id="run_model_call",
            thread_id="term_model_call",
            origin="chat",
            input=Message.user("Inspect the model call"),
        )
        persist_event(
            store,
            StepBegin(
                step=StepPath(run.id, (0,)),
                kind="model",
                input=(),
                given=ModelStepGiven(
                    model="test/model",
                    call=ModelCall(
                        instructions="Historical instructions",
                        messages=[Message.user("Historical message")],
                    ),
                ),
                started_at="2026-08-24T01:00:00Z",
            ),
        )
    finally:
        store.close()

    result = _invoke(
        root,
        "alice",
        "inspect",
        "model_call@run_model_call.0",
        "--json",
    )

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document == {
        "kind": "model_call",
        "target": "model_call@run_model_call.0",
        "state": "historical",
        "model": "test/model",
        "call": {
            "instructions": "Historical instructions",
            "messages": [
                {
                    "role": "user",
                    "parts": [{"type": "text", "text": "Historical message"}],
                }
            ],
            "tools": [],
            "state": None,
        },
        "basis": {
            "run_id": "run_model_call",
            "step_path": "run_model_call.0",
            "preview": False,
        },
        "diagnostics": [],
    }

    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps(_inspect_model_catalog()), encoding="utf-8")
    request_result = _invoke(
        root,
        "--models",
        str(catalog),
        "alice",
        "inspect",
        "model_call@run_model_call.0",
        "--default",
        "model=test/model",
        "--request",
        env={"TEST_API_KEY": "top-secret"},
    )
    assert request_result.exit_code == 0, request_result.stderr
    request = json.loads(request_result.stdout)
    assert request["model"] == "model"
    assert "Historical instructions" in request_result.stdout
    assert "top-secret" not in request_result.stdout

    unused_model = _invoke(
        root,
        "alice",
        "inspect",
        "model_call@run_model_call.0",
        "--default",
        "model=test/model",
    )
    assert unused_model.exit_code == 1
    assert "--default model requires --request" in unused_model.stderr


def test_inspect_prepares_prospective_model_call_and_provider_json(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    layout = AgentLayout.resident(root, "alice")
    (layout.home / "agent.too").write_text(
        """context:
  Context for {{runnable.name}} on {{date}}.

instruct:
  Inspect instruction for {{agent.name}}.

agic review(_: Text, focus: Text):
  models = test/model
  tools = none
  recall = none

  Review {{_}} with focus {{focus}}.
""",
        encoding="utf-8",
    )
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps(_inspect_model_catalog()), encoding="utf-8")
    common = (
        "--models",
        str(catalog),
        "alice",
        "inspect",
        "model_call",
        "--default",
        "runnable=agic:review",
        "--default",
        "model=test/model",
        "--input",
        "draft",
        "--arg",
        "focus=security",
    )
    env = {"TEST_API_KEY": "top-secret"}

    human_result = _invoke(root, *common, env=env)
    call_result = _invoke(root, *common, "--json", env=env)
    request_result = _invoke(
        root,
        *common,
        "--request",
        env=env,
    )
    stdin_result = _invoke(
        root,
        "--models",
        str(catalog),
        "alice",
        "inspect",
        "model_call",
        "--default",
        "runnable=agic:review",
        "--default",
        "model=test/model",
        "--input",
        "-",
        "--arg",
        "focus=security",
        "--json",
        env=env,
        stdin="from standard input\n",
    )

    assert human_result.exit_code == 0, human_result.stderr
    assert "# model call" in human_result.stdout
    assert "# instructions" in human_result.stdout
    assert "Inspect instruction for alice." in human_result.stdout
    assert "# messages" in human_result.stdout
    assert call_result.exit_code == 0, call_result.stderr
    call_document = json.loads(call_result.stdout)
    assert call_document["kind"] == "model_call"
    assert call_document["target"] == "model_call"
    assert call_document["state"] == "prospective"
    assert call_document["model"] == "test/model"
    assert call_document["basis"]["preview"] is True
    assert call_document["basis"]["run_id"] == "<preview-run>"
    assert "Inspect instruction for alice." in call_document["call"]["instructions"]
    assert (
        "Context for review"
        in call_document["call"]["messages"][-1]["parts"][0]["text"]
    )
    assert (
        "Review draft with focus security."
        in (call_document["call"]["messages"][-1]["parts"][0]["text"])
    )
    assert request_result.exit_code == 0, request_result.stderr
    request = json.loads(request_result.stdout)
    assert set(request) >= {"model", "messages", "stream"}
    assert request["model"] == "model"
    assert request["stream"] is True
    assert "kind" not in request
    assert "top-secret" not in request_result.stdout
    assert stdin_result.exit_code == 0, stdin_result.stderr
    stdin_document = json.loads(stdin_result.stdout)
    assert (
        "Review from standard input"
        in (stdin_document["call"]["messages"][-1]["parts"][0]["text"])
    )
    assert not layout.run_store.exists()


def test_inspect_can_include_one_unambiguous_thread_history(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    layout = AgentLayout.resident(root, "alice")
    (layout.home / "agent.too").write_text(
        """agic review(_: Text):
  models = test/model
  tools = none
  recall = history

  Continue {{_}}.
""",
        encoding="utf-8",
    )
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps(_inspect_model_catalog()), encoding="utf-8")
    store = RunStore(layout.run_store)
    try:
        first = project_run_start(
            store,
            run_id="run_history_one",
            thread_id="term_history_one",
            origin="chat",
            input=Message.user("Earlier question"),
        )
        project_step(
            store,
            run_id=first.id,
            step_index=0,
            kind="model",
            status="succeeded",
            input=(),
            output=Message.assistant("Earlier answer").parts,
            started_at="2026-08-24T01:00:00Z",
            finished_at="2026-08-24T01:00:01Z",
        )
        project_run_end(store, run_id=first.id)
    finally:
        store.close()

    result = _invoke(
        root,
        "--models",
        str(catalog),
        "alice",
        "inspect",
        "model_call",
        "--default",
        "runnable=agic:review",
        "--default",
        "model=test/model",
        "--input",
        "next question",
        "--thread",
        "--json",
        env={"TEST_API_KEY": "top-secret"},
    )

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["basis"]["thread_id"] == "term_history_one"
    messages = document["call"]["messages"]
    assert messages[0]["parts"][0]["text"] == "Earlier question"
    assert messages[1]["parts"][0]["text"] == "Earlier answer"

    store = RunStore(layout.run_store)
    try:
        second = project_run_start(
            store,
            run_id="run_history_two",
            thread_id="term_history_two",
            origin="chat",
            input=Message.user("Another question"),
        )
        project_run_end(store, run_id=second.id)
    finally:
        store.close()
    ambiguous = _invoke(
        root,
        "--models",
        str(catalog),
        "alice",
        "inspect",
        "model_call",
        "--default",
        "runnable=agic:review",
        "--default",
        "model=test/model",
        "--input",
        "next question",
        "--thread",
        env={"TEST_API_KEY": "top-secret"},
    )
    assert ambiguous.exit_code == 1
    assert "--thread is ambiguous: multiple threads exist" in ambiguous.stderr


@pytest.mark.parametrize(
    ("options", "message"),
    (
        ((), "prospective model_call requires --default runnable=agic:NAME"),
        (
            ("--request", "--default", "runnable=agic:default"),
            "--request requires --default model=PROVIDER/MODEL_ID",
        ),
        (("--request", "test/model"), "unexpected extra argument"),
        (
            (
                "--request",
                "--default",
                "runnable=agic:default",
                "--default",
                "model=test/model",
                "--json",
            ),
            "cannot be combined",
        ),
        (
            (
                "--request",
                "--default",
                "runnable=agic:default",
                "--default",
                "model=test/model",
                "--full",
            ),
            "cannot be combined",
        ),
        (("--send",), "No such option: --send"),
    ),
)
def test_inspect_rejects_invalid_model_call_view_options(
    tmp_path: Path,
    options: tuple[str, ...],
    message: str,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)

    result = _invoke(
        root,
        "alice",
        "inspect",
        "model_call",
        *options,
    )

    assert result.exit_code != 0
    assert message in result.stderr


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
    assert json.loads(child_result.stdout)["step"]["path"] == "run_child.0"
    assert synthetic_result.exit_code == 1
    assert "step not found: run_parent.0.0" in synthetic_result.stderr


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
    assert document["kind"] == "step"
    assert document["run"]["id"] == "run_roaming"
    assert document["step"]["output"] == {
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
    assert document["kind"] == "step"
    assert document["run"]["id"] == "run_visiting"
    assert document["step"]["output"] == {
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


def _invoke(
    root: Path,
    *args: str,
    tty: bool = False,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
):
    @click.command(
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True}
    )
    @click.argument("arguments", nargs=-1, type=click.UNPROCESSED)
    def public_cli(arguments: tuple[str, ...]) -> None:
        if tty:
            setattr(sys.stderr, "isatty", lambda: True)
        raise click.exceptions.Exit(cli.main(["--root", str(root), *arguments]))

    return runner.invoke(
        public_cli,
        list(args),
        env=dict(env or {}),
        input=stdin,
    )


def _inspect_model_catalog() -> dict[str, object]:
    return {
        "test": {
            "id": "test",
            "name": "Test",
            "env": ["TEST_API_KEY"],
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://api.test/v1",
            "models": {
                "model": {
                    "id": "model",
                    "name": "Model",
                    "attachment": False,
                    "reasoning": False,
                    "tool_call": True,
                    "structured_output": True,
                    "temperature": True,
                    "modalities": {
                        "input": ["text"],
                        "output": ["text"],
                    },
                    "open_weights": False,
                    "limit": {"context": 10_000, "output": 1_000},
                }
            },
        }
    }


def _create_agent(root: Path, name: str = "alice") -> None:
    agents = LocalAgents(root / "agents")
    content = templates.render_template("agent", agent_name=name, name=name)
    if agents.get(name) is not None:
        return
    home = agents.path(name)
    home.mkdir(parents=True, exist_ok=True)
    (home / "agent.too").write_text(content, encoding="utf-8")


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
