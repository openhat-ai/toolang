from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
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
import pytest
from click.testing import CliRunner

from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult, ModelUsage
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.catalog import templates
from toolang.catalog.agent import LocalAgents
from toolang.catalog.job import AuthoredJobs, JobFile
import toolang.cli.toolang.commands.agent as agent_commands
import toolang.cli.toolang.commands.plugin as plugin_commands
import toolang.cli.toolang.commands.thread as thread_commands
import toolang.cli.toolang.main as cli
from toolang.cli.common.output import shorten_home_path
from toolang.cli.common.execution_runtime import ExecutionRuntime
from toolang.common.layout import AgentLayout
from toolang.execution.client import LocalRunClient
from toolang.execution.executor import RunExecutor
from toolang.execution.history import RunHistory
from toolang.execution.records import (
    RerunControlPayload,
    RetryControlPayload,
    SteerControlPayload,
)
from toolang.execution.store import RunStore
from toolang.execution.schemas import RerunRequest, RetryRequest
from toolang.execution.types import Local, Pointer, StepPath, ThreadPrefix
from toolang.lang.input import resolve_input_parts
from toolang.setup import AgentSetup
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
    ("run_root:2.3", "run_root.01", "term_root.0", "run_root^0"),
)
def test_inspect_rejects_invalid_pointers(tmp_path: Path, value: str) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)

    result = _invoke(root, "alice", "inspect", value)

    assert result.exit_code == 2
    assert "invalid pointer" in result.stderr


def test_read_only_thread_commands_do_not_create_execution_store(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    layout = AgentLayout.resident(root, "alice")

    result = _invoke(root, "alice", "threads")

    assert result.exit_code == 0
    assert not layout.run_store.exists()


@pytest.mark.parametrize("schema_version", [11, 18, 24, 27, 31])
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
    assert "requires schema 32" in error_output
    assert "does not migrate" in error_output
    assert "preserve or remove" in error_output.lower()
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


def test_inspect_emits_exact_step_record_json(tmp_path: Path) -> None:
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
            input=(Pointer.control(run.id, 0, "payload", "locals", 0, "value"),),
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
    assert "target" not in document
    assert "run" not in document
    assert "step" not in document

    field = _invoke(
        root,
        "alice",
        "inspect",
        "run_inspect.0/output/value/0",
        "--json",
    )
    human = _invoke(root, "alice", "inspect", "run_inspect.0")
    resolved = _invoke(root, "alice", "inspect", "run_inspect.0/input")
    resolved_value = _invoke(root, "alice", "inspect", "run_inspect.0/input/0")
    status = _invoke(root, "alice", "inspect", "run_inspect.0/status")
    ejected_by = _invoke(root, "alice", "inspect", "run_inspect.0/ejected_by")
    response = _invoke(
        root,
        "alice",
        "inspect",
        "run_inspect.0/output/value",
        "--human",
    )

    assert field.exit_code == 0
    assert json.loads(field.stdout) == {"type": "text", "text": "prepared"}
    assert human.exit_code == 0
    assert human.stdout.splitlines()[0] == "run_inspect.0 has type StepRecord."
    assert "FIELD" in human.stdout
    assert "TYPE" in human.stdout
    assert "StepPath" in human.stdout
    assert "/path" in human.stdout
    assert "/output" in human.stdout
    assert "run_inspect.0/path" not in human.stdout
    assert "run_inspect.0/output" not in human.stdout
    assert '"succeeded"' not in human.stdout
    assert "succeeded" in human.stdout
    assert "• prepared" not in human.stdout
    assert resolved.exit_code == 0
    assert "/0 →" in resolved.stdout
    assert "run_inspect.0/input/0" not in resolved.stdout
    assert "Inspect this" in resolved.stdout
    assert resolved_value.exit_code == 0
    assert resolved_value.stdout.splitlines()[0] == (
        "run_inspect.0/input/0 resolves to Part[]."
    )
    assert status.exit_code == 0
    assert status.stdout == "run_inspect.0/status has type StepStatus.\n\nsucceeded\n"
    assert ejected_by.exit_code == 0
    assert ejected_by.stdout == (
        "run_inspect.0/ejected_by has type ControlRef | None.\n\nnull\n"
    )
    assert response.exit_code == 0
    assert response.stdout.splitlines()[0] == (
        "run_inspect.0/output/value has type Part[]."
    )
    assert response.stdout.count("run_inspect.0/output/value") == 1
    assert "prepared" in response.stdout
    assert "• prepared" not in response.stdout


def test_inspect_display_modes_are_exclusive_and_removed_options_fail(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)

    combined = _invoke(
        root,
        "alice",
        "inspect",
        "run_missing",
        "--human",
        "--json",
    )
    removed_type = _invoke(
        root,
        "alice",
        "inspect",
        "run_missing",
        "--type",
    )
    removed = _invoke(
        root,
        "alice",
        "inspect",
        "run_missing",
        "--limit",
        "1",
    )
    help_code = cli.main(["--root", str(root), "alice", "inspect", "--help"])
    help_output = capsys.readouterr()
    help_text = strip_ansi(help_output.out)

    assert combined.exit_code == 2
    assert "mutually exclusive" in combined.stderr
    assert removed_type.exit_code == 2
    assert "No such option: --type" in strip_ansi(removed_type.stderr)
    assert removed.exit_code == 2
    assert "No such option: --limit" in strip_ansi(removed.stderr)
    assert help_code == 0
    assert "POINTER" in help_text
    assert "--human" in help_text
    assert "--json" in help_text
    assert "--type" not in help_text
    assert "--focus" not in help_text


def test_inspect_human_reports_pointer_resolution_errors(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    store = RunStore(AgentLayout.resident(root, "alice").run_store)
    try:
        run = project_run_start(
            store,
            run_id="run_pointer_errors",
            thread_id="term_pointer_errors",
            origin="test",
            input=Message.user("hello"),
        )
        first = StepPath(run.id, (0,))
        second = StepPath(run.id, (1,))
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=Local.typed("Text", Pointer.step(second, "output", "value"), "_"),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        project_step(
            store,
            run_id=run.id,
            step_index=1,
            kind="value",
            status="succeeded",
            input=(),
            output=Local.typed("Text", Pointer.step(first, "output", "value"), "_"),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        project_step(
            store,
            run_id=run.id,
            step_index=2,
            kind="value",
            status="succeeded",
            input=(),
            output=Local.typed(
                "Text",
                Pointer("run_missing/output/value"),
                "_",
            ),
            started_at="2026-01-01T00:00:02Z",
            finished_at="2026-01-01T00:00:03Z",
        )
        project_step(
            store,
            run_id=run.id,
            step_index=3,
            kind="value",
            status="succeeded",
            input=(),
            output=Local.typed(
                "Text",
                Pointer.step(StepPath(run.id, (4,)), "output", "value", 0),
                "_",
            ),
            started_at="2026-01-01T00:00:03Z",
            finished_at="2026-01-01T00:00:04Z",
        )
        project_step(
            store,
            run_id=run.id,
            step_index=4,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("part, not text"),),
            started_at="2026-01-01T00:00:04Z",
            finished_at="2026-01-01T00:00:05Z",
        )
    finally:
        store.close()

    cycle = _invoke(
        root,
        "alice",
        "inspect",
        f"{first}/output/value",
    )
    missing = _invoke(
        root,
        "alice",
        "inspect",
        f"{run.id}.2/output/value",
    )
    mismatch = _invoke(
        root,
        "alice",
        "inspect",
        f"{run.id}.3/output/value",
    )

    assert cycle.exit_code == 1
    assert "Pointer cycle" in cycle.stderr
    assert f"{first}/output/value" in cycle.stderr
    assert f"{second}/output/value" in cycle.stderr
    assert missing.exit_code == 1
    assert "record not found: run_missing" in missing.stderr
    assert mismatch.exit_code == 1
    assert "is not Text" in mismatch.stderr


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
        ("run", "immediate", "applied"),
        ("steer", "next_step", "pending"),
        ("cancel", "immediate", "pending"),
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

    state_reads: list[None] = []
    runtime_selections: list[str | None] = []

    def current_state():
        state_reads.append(None)
        return harness.state

    def load_state(revision: str):
        if harness.state.revision != revision:
            raise ValueError(f"snapshot revision not found: {revision}")
        return harness.state

    @contextmanager
    def execution_runtime(
        _layout: AgentLayout,
        *,
        sandbox: str | None,
        **_kwargs: object,
    ):
        runtime_selections.append(sandbox)
        yield ExecutionRuntime(sandbox=sandbox or "host", mode="embedded")

    @asynccontextmanager
    async def run_client(
        _layout: AgentLayout,
        _runtime: ExecutionRuntime,
        **_kwargs: object,
    ):
        executor = RunExecutor(
            harness.store,
            harness.ids,
            setup=lambda: harness.setup,
            state=current_state,
            load_state=load_state,
            include=lambda _setup: lambda _reference: TextPart("unused"),
        )
        client = LocalRunClient(executor)
        await client.connect()
        try:
            yield client
        finally:
            await client.disconnect()
            await executor.stop()

    monkeypatch.setattr(thread_commands, "open_execution_runtime", execution_runtime)
    monkeypatch.setattr(thread_commands, "open_run_client", run_client)

    async def run_source():
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
        source = await harness.executor.run(
            harness.run_spec(
                thread=thread,
                runnable="reply",
                primary=resolve_input_parts("hello"),
            )
        )
        await harness.executor.stop()
        return source

    try:
        source = asyncio.run(run_source())
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
        assert state_reads == []
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
        assert state_reads == [None]
        assert runtime_selections == ["host", None]
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
            assert retry_footer.startswith(f"∎ {source.id}: retry succeeded  ")
            assert "succeeded ·" not in retry_footer

            assert rerun.stdout == ""
            rerun_output = strip_ansi(rerun.stderr)
            assert "• reran" in rerun_output
            rerun_footer = next(
                line
                for line in rerun_output.splitlines()
                if f"{rerun_id}: rerun succeeded" in line
            )
            assert rerun_footer.startswith(f"∎ {rerun_id}: rerun succeeded  ")
            assert "succeeded ·" not in rerun_footer
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


def test_retry_and_rerun_use_the_remote_run_client_for_an_active_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        responses=[ModelCallResult(message=Message.assistant("source"))],
    )
    _create_agent(harness.setup.layout.root)
    observed_requests: list[RetryRequest | RerunRequest] = []
    runtime_selections: list[tuple[str | None, Path | None]] = []

    async def run_source():
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
        source = await harness.executor.run(
            harness.run_spec(
                thread=thread,
                runnable="reply",
                primary=resolve_input_parts("hello"),
            )
        )
        await harness.executor.stop()
        return source

    source = asyncio.run(run_source())
    source_detail = RunHistory(harness.store).get_run(source.id)
    assert source_detail is not None
    rerun_detail = replace(
        source_detail,
        id="run_remote_rerun",
        root_run_id="run_remote_rerun",
    )

    class _Handle:
        def __init__(self, detail):
            self.run_id = detail.id
            self.detail = detail

        async def wait(self):
            return self.detail

    class _Client:
        async def retry(self, request, *, tracer=None):
            del tracer
            observed_requests.append(request)
            return _Handle(source_detail)

        async def rerun(self, request, *, tracer=None):
            del tracer
            observed_requests.append(request)
            return _Handle(rerun_detail)

    @contextmanager
    def execution_runtime(
        _layout: AgentLayout,
        *,
        sandbox: str | None,
        dev: Path | None,
        **_kwargs: object,
    ):
        runtime_selections.append((sandbox, dev))
        yield ExecutionRuntime(
            sandbox=sandbox or "host",
            mode="remote",
            endpoint="http://runtime.test",
        )

    @asynccontextmanager
    async def run_client(
        _layout: AgentLayout,
        _runtime: ExecutionRuntime,
        **_kwargs: object,
    ):
        yield _Client()

    monkeypatch.setattr(thread_commands, "open_execution_runtime", execution_runtime)
    monkeypatch.setattr(thread_commands, "open_run_client", run_client)
    dev = tmp_path / "dist"
    dev.mkdir()

    try:
        retry = _invoke(
            harness.setup.layout.root,
            "alice",
            "retry",
            source.id,
            "--dev",
            str(dev),
            "--limit",
            "time=10",
        )
        rerun = _invoke(
            harness.setup.layout.root,
            "alice",
            "rerun",
            source.id,
            "--sandbox",
            "docker:python:3.13-slim",
            "--dev",
            str(dev),
            "--limit",
            "time=20",
        )

        assert retry.exit_code == rerun.exit_code == 0
        assert runtime_selections == [
            ("host", dev),
            ("docker:python:3.13-slim", dev),
        ]
        assert len(observed_requests) == 2
        assert isinstance(observed_requests[0], RetryRequest)
        assert observed_requests[0].source == source.id
        assert observed_requests[0].commands[-1].value == 10
        assert isinstance(observed_requests[1], RerunRequest)
        assert observed_requests[1].source == source.id
        assert observed_requests[1].commands[-1].value == 20
        assert retry.stdout.strip() == f"retried {source.id}: succeeded"
        assert rerun.stdout.strip() == (
            f"reran {source.id} as {rerun_detail.id}: succeeded"
        )
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
    toolset = "shell"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description="Echo text.")

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del context
        return dict(arguments)
