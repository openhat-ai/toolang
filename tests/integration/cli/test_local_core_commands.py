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
from toolang.execution.types import (
    ControlRef,
    Local,
    ModelStepGiven,
    Pointer,
    StepPath,
    ThreadPrefix,
)
from toolang.lang.input import resolve_input_parts
from toolang.setup import AgentSetup
from toolang.up import process as agents
from toolang.up.types import AgentServerRef
from toolang.work.state import load_ready_jobs
from toolang.work.store import JobStore
from tests.support.execution_fixtures import (
    project_run_control,
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


def test_inspect_missing_history_does_not_create_execution_store(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    layout = AgentLayout.resident(root, "alice")

    result = _invoke(root, "alice", "inspect", "threads")

    assert result.exit_code == 1
    assert "execution history not found: alice" in result.stderr
    assert not layout.run_store.exists()


@pytest.mark.parametrize("command", ("threads", "runs"))
def test_removed_history_commands_are_unavailable(
    tmp_path: Path,
    command: str,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    layout = AgentLayout.resident(root, "alice")

    result = _invoke(root, "alice", command)

    assert result.exit_code == 2
    assert "No such command" in result.stderr
    assert not layout.run_store.exists()


def test_inspect_thread_and_run_collections_read_local_history(tmp_path: Path) -> None:
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

    threads = _invoke(root, "alice", "inspect", "threads")
    runs = _invoke(root, "alice", "inspect", "term_main", "runs")

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
    raw_pointer = _invoke(
        root,
        "alice",
        "inspect",
        "run_inspect.0/input/0",
        "--json",
    )
    human_thread = _invoke(root, "alice", "inspect", "term_inspect")
    human_run = _invoke(root, "alice", "inspect", "run_inspect")
    human_control = _invoke(root, "alice", "inspect", "run_inspect@0")
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
    assert raw_pointer.exit_code == 0, raw_pointer.stderr
    assert json.loads(raw_pointer.stdout) == document["input"][0]
    assert human_thread.exit_code == 0, human_thread.stderr
    assert "THREAD FIELD TYPE VALUE" in " ".join(
        strip_ansi(human_thread.stdout).split()
    )
    assert human_run.exit_code == 0, human_run.stderr
    assert "RUN FIELD TYPE VALUE" in " ".join(strip_ansi(human_run.stdout).split())
    assert human_control.exit_code == 0, human_control.stderr
    assert "CONTROL FIELD TYPE VALUE" in " ".join(
        strip_ansi(human_control.stdout).split()
    )
    assert human.exit_code == 0
    assert "STEP FIELD TYPE VALUE" in " ".join(strip_ansi(human.stdout).split())
    assert "has type" not in human.stdout
    assert "append a FIELD" not in human.stdout
    assert "TYPE" in human.stdout
    assert "StepPath" in human.stdout
    assert "ControlRef?" in human.stdout
    assert "ControlRef | None" not in human.stdout
    assert "/path" in human.stdout
    assert "/output" in human.stdout
    assert "run_inspect.0/path" not in human.stdout
    assert "run_inspect.0/output" not in human.stdout
    assert '"succeeded"' not in human.stdout
    assert "succeeded" in human.stdout
    assert "• prepared" not in human.stdout
    assert resolved.exit_code == 0
    resolved_lines = [
        " ".join(line.split()) for line in strip_ansi(resolved.stdout).splitlines()
    ]
    assert "POINTER[] FIELD TYPE VALUE" in resolved_lines
    resolved_row = next(line for line in resolved_lines if line.startswith("/0 "))
    assert resolved_row.split()[:2] == ["/0", "*Part[]"]
    assert "→" not in resolved.stdout
    assert "run_inspect.0/input/0" not in resolved.stdout
    assert "Inspect this" in resolved.stdout
    assert resolved_value.exit_code == 0
    assert "resolves to" not in resolved_value.stdout
    assert "run_inspect.0/input/0" not in resolved_value.stdout
    assert strip_ansi(resolved_value.stdout).strip() == "Inspect this"
    assert status.exit_code == 0
    assert status.stdout == "succeeded\n"
    assert ejected_by.exit_code == 0
    assert ejected_by.stdout == "null\n"
    assert response.exit_code == 0
    assert "run_inspect.0/output/value" not in response.stdout
    assert "prepared" in response.stdout
    assert "• prepared" not in response.stdout


def test_inspect_lists_root_and_related_record_subjects(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    store = RunStore(AgentLayout.resident(root, "alice").run_store)
    try:
        first = project_run_start(
            store,
            run_id="run_subject_first",
            thread_id="custom_subject",
            origin="test",
            input=Message.user("First"),
            created_at="2026-01-01T00:00:00Z",
        )
        project_step(
            store,
            run_id=first.id,
            step_index=2,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("outer"),),
            started_at="2026-01-01T00:00:02Z",
            finished_at="2026-01-01T00:00:03Z",
        )
        project_step(
            store,
            parent=StepPath(first.id, (2,)),
            index=10,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("nested"),),
            started_at="2026-01-01T00:00:03Z",
            finished_at="2026-01-01T00:00:04Z",
        )
        project_step(
            store,
            run_id=first.id,
            step_index=11,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("last"),),
            started_at="2026-01-01T00:00:04Z",
            finished_at="2026-01-01T00:00:05Z",
        )
        project_run_end(store, run_id=first.id)
        child = project_run_start(
            store,
            run_id="run_subject_child",
            thread_id=first.thread,
            origin="test",
            input=Message.user("Child"),
            created_at="2026-01-01T12:00:00Z",
            parent=StepPath(first.id, (2,)),
        )
        project_run_end(store, run_id=child.id)
        second = project_run_start(
            store,
            run_id="run_subject_second",
            thread_id=first.thread,
            origin="test",
            input=Message.user("Second"),
            created_at="2026-01-02T00:00:00Z",
        )
        project_run_end(store, run_id=second.id)
        unrelated = project_run_start(
            store,
            run_id="run_subject_other",
            thread_id="term_subject_other",
            origin="test",
            input=Message.user("Other"),
            created_at="2026-01-03T00:00:00Z",
        )
        project_run_end(store, run_id=unrelated.id)
        store.create_thread(
            thread_id="threads",
            origin="test",
            created_at="2025-12-31T00:00:00Z",
        )
    finally:
        store.close()

    threads = _invoke(root, "alice", "inspect", "threads", "--json")
    human_threads = _invoke(root, "alice", "inspect", "threads")
    runs = _invoke(root, "alice", "inspect", "runs", "--json")
    scoped_runs = _invoke(
        root,
        "alice",
        "inspect",
        "custom_subject",
        "runs",
        "--json",
    )
    human_runs = _invoke(root, "alice", "inspect", "runs")
    human_scoped_runs = _invoke(
        root,
        "alice",
        "inspect",
        "custom_subject",
        "runs",
    )
    steps = _invoke(
        root,
        "alice",
        "inspect",
        first.id,
        "steps",
        "--json",
    )

    assert threads.exit_code == 0, threads.stderr
    assert [item["thread_id"] for item in json.loads(threads.stdout)] == [
        "term_subject_other",
        "custom_subject",
        "threads",
    ]
    assert human_threads.exit_code == 0, human_threads.stderr
    human_thread_lines = [
        " ".join(line.split()) for line in strip_ansi(human_threads.stdout).splitlines()
    ]
    assert "THREAD TITLE RUNS STATUS UPDATED" in human_thread_lines
    assert runs.exit_code == 0, runs.stderr
    assert [item["id"] for item in json.loads(runs.stdout)] == [
        "run_subject_other",
        "run_subject_second",
        "run_subject_child",
        "run_subject_first",
    ]
    assert all("steps" not in item for item in json.loads(runs.stdout))
    assert scoped_runs.exit_code == 0, scoped_runs.stderr
    assert [item["id"] for item in json.loads(scoped_runs.stdout)] == [
        "run_subject_second",
        "run_subject_child",
        "run_subject_first",
    ]
    assert human_runs.exit_code == 0, human_runs.stderr
    human_run_lines = [
        " ".join(line.split()) for line in strip_ansi(human_runs.stdout).splitlines()
    ]
    assert "RUN THREAD TITLE STEPS STATUS CREATED" in human_run_lines
    first_run_line = next(
        line for line in human_run_lines if line.startswith("run_subject_first ")
    )
    child_run_line = next(
        line for line in human_run_lines if line.startswith("run_subject_child ")
    )
    assert first_run_line.split()[-3:] == [
        "3",
        "succeeded",
        "2026-01-01T00:00:00Z",
    ]
    assert child_run_line.split()[-3:] == [
        "0",
        "succeeded",
        "2026-01-01T12:00:00Z",
    ]
    assert human_scoped_runs.exit_code == 0, human_scoped_runs.stderr
    human_scoped_lines = [
        " ".join(line.split())
        for line in strip_ansi(human_scoped_runs.stdout).splitlines()
    ]
    assert "THREAD RUN TITLE STEPS STATUS CREATED" in human_scoped_lines
    scoped_first_line = next(
        line for line in human_scoped_lines if line.startswith("run_subject_first ")
    )
    assert scoped_first_line.split()[-3:] == [
        "3",
        "succeeded",
        "2026-01-01T00:00:00Z",
    ]
    assert steps.exit_code == 0, steps.stderr
    step_documents = json.loads(steps.stdout)
    assert [item["path"] for item in step_documents] == [
        "run_subject_first.2",
        "run_subject_first.2.10",
        "run_subject_first.11",
    ]
    for document in step_documents:
        selected = _invoke(
            root,
            "alice",
            "inspect",
            document["path"],
            "--json",
        )
        assert selected.exit_code == 0, selected.stderr
        assert json.loads(selected.stdout) == document

    human = _invoke(root, "alice", "inspect", first.id, "steps")
    assert human.exit_code == 0, human.stderr
    human_step_lines = [
        " ".join(line.split()) for line in strip_ansi(human.stdout).splitlines()
    ]
    assert "RUN STEP KIND STATUS CREATED" in human_step_lines
    assert (
        human.stdout.index("run_subject_first.2")
        < human.stdout.index("run_subject_first.2.10")
        < human.stdout.index("run_subject_first.11")
    )


def test_inspect_lists_mixed_control_subjects(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    store = RunStore(AgentLayout.resident(root, "alice").run_store)
    try:
        run = project_run_start(
            store,
            run_id="run_control_subject",
            thread_id="term_control_subject",
            origin="test",
            input=Message.user("Control"),
            created_at="2026-01-01T00:00:00Z",
        )
        project_run_control(
            store,
            run_id=run.id,
            kind="steer",
            input=Message.user("Continue"),
            created_at="2026-01-01T01:00:00Z",
        )
        store.create_thread(
            thread_id="controls",
            origin="test",
            created_at="2026-01-01T02:00:00Z",
        )
    finally:
        store.close()

    result = _invoke(root, "alice", "inspect", "controls", "--json")
    human = _invoke(root, "alice", "inspect", "controls")
    rejected = _invoke(root, "alice", "inspect", "controls", "runs")

    assert result.exit_code == 0, result.stderr
    documents = json.loads(result.stdout)
    pointers = [f"{item['target']}@{item['index']}" for item in documents]
    assert pointers == [
        "controls@0",
        "run_control_subject@1",
        "run_control_subject@0",
        "term_control_subject@0",
    ]
    assert all("scope" not in item for item in documents)
    for pointer, document in zip(pointers, documents, strict=True):
        selected = _invoke(root, "alice", "inspect", pointer, "--json")
        assert selected.exit_code == 0, selected.stderr
        assert json.loads(selected.stdout) == document

    assert human.exit_code == 0, human.stderr
    human_lines = [
        " ".join(line.split()) for line in strip_ansi(human.stdout).splitlines()
    ]
    assert "CONTROL KIND STATUS CREATED" in human_lines
    assert all(pointer in human.stdout for pointer in pointers)
    assert rejected.exit_code == 2
    assert "controls does not accept a child subject" in rejected.stderr


def test_inspect_collections_are_unbounded_and_empty_stores_succeed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    layout = AgentLayout.resident(root, "alice")
    empty = RunStore(layout.run_store)
    empty.close()

    empty_threads = _invoke(root, "alice", "inspect", "threads", "--json")
    empty_runs = _invoke(root, "alice", "inspect", "runs")
    empty_controls = _invoke(root, "alice", "inspect", "controls", "--json")

    assert empty_threads.exit_code == 0, empty_threads.stderr
    assert json.loads(empty_threads.stdout) == []
    assert empty_runs.exit_code == 0, empty_runs.stderr
    assert "RUN" in empty_runs.stdout
    assert "STEPS" in empty_runs.stdout
    assert empty_controls.exit_code == 0, empty_controls.stderr
    assert json.loads(empty_controls.stdout) == []

    store = RunStore(layout.run_store)
    try:
        for index in range(51):
            run = project_run_start(
                store,
                run_id=f"run_unbounded_{index}",
                thread_id=f"term_unbounded_{index}",
                origin="test",
                input=Message.user(str(index)),
                created_at=f"2026-01-01T00:00:{index:02}Z",
            )
            project_run_end(store, run_id=run.id)
    finally:
        store.close()

    threads = _invoke(root, "alice", "inspect", "threads", "--json")
    runs = _invoke(root, "alice", "inspect", "runs", "--json")
    controls = _invoke(root, "alice", "inspect", "controls", "--json")

    assert threads.exit_code == 0, threads.stderr
    assert len(json.loads(threads.stdout)) == 51
    assert runs.exit_code == 0, runs.stderr
    assert len(json.loads(runs.stdout)) == 51
    assert controls.exit_code == 0, controls.stderr
    assert len(json.loads(controls.stdout)) == 102


def test_inspect_human_collection_uses_resolved_run_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    store = RunStore(AgentLayout.resident(root, "alice").run_store)
    try:
        run = project_run_start(
            store,
            run_id="run_single_query",
            thread_id="term_single_query",
            origin="test",
            input=Message.user("One query"),
        )
        project_run_end(store, run_id=run.id)
    finally:
        store.close()

    original = RunStore.list_runs
    calls = 0
    original_steps = RunStore.list_steps_for_runs
    step_calls = 0

    def list_runs_once(instance: RunStore, **kwargs: Any):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("inspect must not re-query its Run collection")
        return original(instance, **kwargs)

    monkeypatch.setattr(RunStore, "list_runs", list_runs_once)

    def list_steps_once(instance: RunStore, **kwargs: Any):
        nonlocal step_calls
        step_calls += 1
        if step_calls > 1:
            raise AssertionError("inspect must load its Step counts only once")
        return original_steps(instance, **kwargs)

    monkeypatch.setattr(RunStore, "list_steps_for_runs", list_steps_once)

    result = _invoke(root, "alice", "inspect", "runs")

    assert result.exit_code == 0, result.stderr
    assert "run_single_query" in result.stdout
    assert calls == 1
    assert step_calls == 1

    calls = 0
    step_calls = 0
    monkeypatch.setattr(
        RunHistory,
        "describe_runs",
        lambda *_args, **_kwargs: pytest.fail(
            "JSON collection output must not build human summaries"
        ),
    )

    json_result = _invoke(root, "alice", "inspect", "runs", "--json")

    assert json_result.exit_code == 0, json_result.stderr
    assert [item["id"] for item in json.loads(json_result.stdout)] == [
        "run_single_query"
    ]
    assert calls == 1
    assert step_calls == 0


@pytest.mark.parametrize(
    ("subjects", "allowed"),
    (
        (("custom_subject", "steps"), "runs"),
        (("run_subject", "runs"), "steps"),
        (("run_subject.0", "steps"), "model-call"),
        (("run_subject/status", "steps"), None),
    ),
)
def test_inspect_rejects_disallowed_subject_transitions(
    tmp_path: Path,
    subjects: tuple[str, ...],
    allowed: str | None,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    store = RunStore(AgentLayout.resident(root, "alice").run_store)
    try:
        run = project_run_start(
            store,
            run_id="run_subject",
            thread_id="custom_subject",
            origin="test",
            input=Message.user("Subject"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("value"),),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
    finally:
        store.close()

    result = _invoke(root, "alice", "inspect", *subjects)

    assert result.exit_code == 2
    if allowed is None:
        assert "does not accept a child subject" in result.stderr
    else:
        assert f"allowed: {allowed}" in result.stderr


def test_inspect_static_subjects_are_reserved_and_missing_scopes_fail(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    store = RunStore(AgentLayout.resident(root, "alice").run_store)
    store.close()

    reserved = _invoke(root, "alice", "inspect", "steps")
    missing_thread = _invoke(root, "alice", "inspect", "custom_missing", "runs")
    missing_run = _invoke(root, "alice", "inspect", "run_missing", "steps")

    assert reserved.exit_code == 2
    assert "allowed: threads, runs, controls" in reserved.stderr
    assert missing_thread.exit_code == 1
    assert "record not found: custom_missing" in missing_thread.stderr
    assert missing_run.exit_code == 1
    assert "record not found: run_missing" in missing_run.stderr


@pytest.mark.parametrize(
    "subject", ("threads", "runs", "controls", "term_missing runs")
)
def test_inspect_collections_require_execution_history(
    tmp_path: Path,
    subject: str,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)

    result = _invoke(root, "alice", "inspect", *subject.split())

    assert result.exit_code == 1
    assert "execution history not found: alice" in result.stderr


def test_inspect_projects_complete_persisted_model_call(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    store = RunStore(AgentLayout.resident(root, "alice").run_store)
    try:
        run = project_run_start(
            store,
            run_id="run_model_call",
            thread_id="term_model_call",
            origin="test",
            input=Message.user("Call"),
        )
        structured_output: dict[str, object] = {
            "$defs": {
                "Answer": {
                    "additionalProperties": False,
                    "properties": {"answer": {"type": "boolean"}},
                    "required": ["answer"],
                    "type": "object",
                }
            },
            "$ref": "#/$defs/Answer",
        }
        call = ModelCall(
            instructions="Diagnose the run.",
            messages=[Message.assistant("Context"), Message.user("Question")],
            structured_output=structured_output,
            continuation={"provider_cursor": "next"},
        )
        store.begin_step(
            path=StepPath(run.id, (0,)),
            kind="model",
            input=(),
            given=ModelStepGiven(model="test/model", call=call),
            state=ControlRef(run.id, 0),
            started_at="2026-01-01T00:00:00Z",
        )
        store.finish_step(
            path=StepPath(run.id, (0,)),
            kind="model",
            status="succeeded",
            output=Local.typed(
                "Part[]",
                (TextPart("Complete answer"),),
                "_",
                0,
            ),
            noted=None,
            error=None,
            finished_at="2026-01-01T00:00:01Z",
        )
    finally:
        store.close()

    projected = _invoke(
        root,
        "alice",
        "inspect",
        "run_model_call.0",
        "model-call",
        "--json",
    )
    references = _invoke(
        root,
        "alice",
        "inspect",
        "run_model_call.0/given/call",
        "--json",
    )
    human = _invoke(
        root,
        "alice",
        "inspect",
        "run_model_call.0",
        "model-call",
        "--human",
    )
    rejected = _invoke(
        root,
        "alice",
        "inspect",
        "run_model_call",
        "model-call",
    )

    assert projected.exit_code == 0, projected.stderr
    assert json.loads(projected.stdout) == {
        "instructions": "Diagnose the run.",
        "messages": [
            {
                "role": "assistant",
                "parts": [{"type": "text", "text": "Context"}],
            },
            {
                "role": "user",
                "parts": [{"type": "text", "text": "Question"}],
            },
        ],
        "tools": [],
        "structured_output": structured_output,
        "cont": {"provider_cursor": "next"},
    }
    assert references.exit_code == 0, references.stderr
    assert json.loads(references.stdout) != json.loads(projected.stdout)
    assert human.exit_code == 0, human.stderr
    human_output = strip_ansi(human.stdout)
    assert "Instructions" in human_output
    assert "Diagnose the run." in human_output
    assert "Messages 2" in human_output
    assert "[2] assistant" in human_output
    assert "Context" in human_output
    assert "[1] user" in human_output
    assert "Question" in human_output
    assert "[=] assistant" in human_output
    assert "[0] assistant" not in human_output
    assert "assistant · result" not in human_output
    assert "Complete answer" in human_output
    assert "Tools 0" in human_output
    assert "No available tools." in human_output
    assert "Structured Output" in human_output
    assert json.dumps(structured_output, ensure_ascii=False, indent=2) in human_output
    assert "Continuation" in human_output
    assert "provider_cursor" in human_output
    assert '"instructions":' not in human_output
    assert "projected as model-call" not in human_output
    assert rejected.exit_code == 2
    assert "does not support projector model-call" in rejected.stderr

    connection = sqlite3.connect(AgentLayout.resident(root, "alice").run_store)
    try:
        row = connection.execute(
            "SELECT given FROM steps WHERE run = ? AND path = ?",
            ("run_model_call", "0"),
        ).fetchone()
        assert row is not None
        given = json.loads(row[0])
        given["call"].pop("structured_output")
        connection.execute(
            "UPDATE steps SET given = ? WHERE run = ? AND path = ?",
            (json.dumps(given), "run_model_call", "0"),
        )
        connection.commit()
    finally:
        connection.close()

    legacy = _invoke(
        root,
        "alice",
        "inspect",
        "run_model_call.0",
        "model-call",
        "--json",
    )

    assert legacy.exit_code == 0, legacy.stderr
    assert json.loads(legacy.stdout)["structured_output"] is None


@pytest.mark.parametrize(
    "projector",
    ("modelcall", "model_call", "records", "fields", "value"),
)
def test_inspect_rejects_unregistered_projector_spellings(
    tmp_path: Path,
    projector: str,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    store = RunStore(AgentLayout.resident(root, "alice").run_store)
    try:
        run = project_run_start(
            store,
            run_id="run_projector_spelling",
            thread_id="term_projector_spelling",
            origin="test",
            input=Message.user("Call"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("value"),),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
    finally:
        store.close()

    spelling = _invoke(
        root,
        "alice",
        "inspect",
        "run_projector_spelling.0",
        projector,
    )
    non_model = _invoke(
        root,
        "alice",
        "inspect",
        "run_projector_spelling.0",
        "model-call",
    )

    assert spelling.exit_code == 2
    compact_error = " ".join(strip_ansi(spelling.stderr).replace("│", "").split())
    assert "allowed: model-call" in compact_error
    assert non_model.exit_code == 1
    assert "step is not a model call: run_projector_spelling.0" in non_model.stderr


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
    compact_help = " ".join(help_text.replace("│", "").split())

    assert combined.exit_code == 2
    assert "mutually exclusive" in combined.stderr
    assert removed_type.exit_code == 2
    assert "No such option: --type" in strip_ansi(removed_type.stderr)
    assert removed.exit_code == 2
    assert "No such option: --limit" in strip_ansi(removed.stderr)
    assert help_code == 0
    assert "POINTER" in help_text
    assert "Inspect execution subjects." in help_text
    assert "Subject chain." in help_text
    assert "Root subjects: threads, runs, controls" in compact_help
    assert "THREAD runs" in compact_help
    assert "RUN steps" in compact_help
    assert "STEP model-call" in compact_help
    assert "Render human-readable output (default)." in help_text
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


def test_roaming_source_reads_inspect_collections_and_records(
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

    threads = cli.main([str(source), "inspect", "threads"])
    threads_output = capsys.readouterr()
    runs = cli.main([str(source), "inspect", "script_roaming", "runs"])
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
    def agent_server_context(
        _layout: AgentLayout,
        *,
        sandbox: str | None,
        **_kwargs: object,
    ):
        runtime_selections.append(sandbox)
        yield None

    @asynccontextmanager
    async def run_client(
        _layout: AgentLayout,
        _server: AgentServerRef | None,
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

    monkeypatch.setattr(thread_commands, "acquire_agent_server", agent_server_context)
    monkeypatch.setattr(thread_commands, "acquire_run_client", run_client)

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
    def agent_server_context(
        _layout: AgentLayout,
        *,
        sandbox: str | None,
        dev: Path | None,
        **_kwargs: object,
    ):
        runtime_selections.append((sandbox, dev))
        yield AgentServerRef(
            sandbox=sandbox or "host",
            endpoint="http://runtime.test",
        )

    @asynccontextmanager
    async def run_client(
        _layout: AgentLayout,
        _server: AgentServerRef | None,
        **_kwargs: object,
    ):
        yield _Client()

    monkeypatch.setattr(thread_commands, "acquire_agent_server", agent_server_context)
    monkeypatch.setattr(thread_commands, "acquire_run_client", run_client)
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


@pytest.mark.parametrize(
    ("command", "group", "header", "plugin_name", "empty_message"),
    (
        (
            "catalogs",
            "toolang.model_catalog",
            "CATALOG",
            "models_dev",
            "No catalogs found.",
        ),
        (
            "toolsets",
            "toolang.toolset",
            "TOOLSET",
            "shell",
            "No toolsets found.",
        ),
        (
            "sandboxes",
            "toolang.sandbox",
            "SANDBOX",
            "docker",
            "No sandboxes found.",
        ),
    ),
)
def test_plugin_inventory_commands_list_entry_points_and_handle_empty_groups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    group: str,
    header: str,
    plugin_name: str,
    empty_message: str,
) -> None:
    root = tmp_path / "toolang"
    requested: list[str] = []

    def plugin_rows(selected_group: str) -> list[tuple[str, str]]:
        requested.append(selected_group)
        return [(plugin_name, "external")]

    monkeypatch.setattr(
        plugin_commands,
        "plugin_info_rows",
        plugin_rows,
    )

    result = _invoke(root, command)

    assert result.exit_code == 0
    output = strip_ansi(result.stdout)
    assert header in output
    assert "SOURCE" in output
    assert plugin_name in output
    assert "external" in output
    assert requested == [group]

    monkeypatch.setattr(plugin_commands, "plugin_info_rows", lambda _group: [])
    empty = _invoke(root, command)

    assert empty.exit_code == 0
    assert empty.stdout.strip() == empty_message


@pytest.mark.parametrize("command", ("catalogs", "toolsets"))
def test_plugin_inventory_commands_have_only_direct_plural_forms(
    tmp_path: Path,
    command: str,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)

    singular = _invoke(root, command[:-1])
    nested = _invoke(root, command, "list")
    targeted = _invoke(root, "alice", command)

    assert singular.exit_code == 2
    assert nested.exit_code == 2
    assert "unexpected extra argument" in nested.stderr.lower()
    assert targeted.exit_code == 2
    assert f"{command} does not accept an agent target here" in targeted.stderr


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
