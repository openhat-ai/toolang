"""Flow correctness scenarios composed from public execution objects."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path

import pytest

from tests.support.execution_assertions import (
    assert_run_event_integrity,
    event_labels,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingTool,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.run import ModelCallResult, ModelUsage
from toolang.execution.events import RunBegin, RunEnd
from toolang.execution.executor import RunLimits
from toolang.execution.history import RunHistory
from toolang.execution.records import (
    RerunControlPayload,
    RetryControlPayload,
    RunControlPayload,
)
from toolang.execution.types import (
    CollectionStepNoted,
    IterationOccurrence,
    Local,
    LoopStepNoted,
    Occurrence,
    OccurrencePosition,
    StepPath,
    ThreadPrefix,
    Pointer,
    TypedPointer,
)
from toolang.lang.input import resolve_input_parts
from toolang.lang.types import Array
from toolang.state.prepare import prepare_agent_state


def _output_value(harness: ExecutionHarness, run_id: str) -> object:
    return json.loads(harness.store.run_output_text(run_id=run_id))


def _root_step_kinds(
    harness: ExecutionHarness,
    run_id: str,
) -> list[str]:
    return [
        step.kind
        for step in harness.store.list_steps(run_id=run_id)
        if step.parent is None
    ]


def test_model_free_flow_retry_preserves_an_absent_model_request(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow passthrough(_: Text) -> Text:
  let note:
    retained
""",
        responses=[],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:passthrough",
                    primary=resolve_input_parts("hello"),
                )
            )
            original = harness.store.list_run_controls(run_id=root.id)[0]
            assert isinstance(original.payload, RunControlPayload)
            assert original.payload.model_request is None

            retried = await harness.executor.retry(
                root.id,
                setup=harness.setup,
                state=harness.state,
            )

            assert retried.status == "succeeded"
            retry = harness.store.list_run_controls(run_id=root.id)[-1]
            assert isinstance(retry.payload, RetryControlPayload)
            assert retry.payload.model_request is None

    asyncio.run(scenario())


def test_flow_calls_an_agic_as_a_nested_run(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic echo(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow relay(_: Part[]) -> Part[]:
  run echo
""",
        responses=[ModelCallResult(message=Message.assistant("relayed"))],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="relay",
                    primary=resolve_input_parts("hello"),
                ),
                tracer=tracer,
            )

            runs = harness.store.list_runs(thread_id=thread, limit=None)
            children = [run for run in runs if run.parent is not None]
            assert root.status == "succeeded"
            assert len(children) == 1
            child = children[0]
            assert child.status == "succeeded"
            assert child.parent == StepPath.parse(f"{root.id}.0")
            assert harness.store.root_run_id(run_id=child.id) == root.id
            child_run_control = harness.store.get_run_control(run_id=child.id, index=0)
            root_run_control = harness.store.get_run_control(run_id=root.id, index=0)
            assert child_run_control is not None
            assert root_run_control is not None
            assert isinstance(child_run_control.payload, RunControlPayload)
            assert isinstance(root_run_control.payload, RunControlPayload)
            assert child_run_control.payload.runnable == "agic:echo"
            assert child_run_control.payload.sandbox is None
            assert root_run_control.payload.sandbox == "host"
            assert harness.store.run_output(run_id=root.id) == (TextPart("relayed"),)
            assert _root_step_kinds(harness, root.id) == ["run"]
            assert [
                step.kind for step in harness.store.list_steps(run_id=child.id)
            ] == ["model"]
            run_events = [
                event for event in tracer.events if isinstance(event, RunBegin | RunEnd)
            ]
            assert [(event.type, event.run) for event in run_events] == [
                ("run_begin", root.id),
                ("run_begin", child.id),
                ("run_end", child.id),
                ("run_end", root.id),
            ]

    asyncio.run(scenario())


def test_runtime_content_prompts_append_durable_provenance(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
prompt bracket:
  [{{_}}]

agic echo(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user:
    $bracket -- {{_}}

flow relay(_: Part[]) -> Part[]:
  let note:
    $bracket -- flow
  run echo
""",
        responses=[ModelCallResult(message=Message.assistant("relayed"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="relay",
                    primary=resolve_input_parts("hello"),
                )
            )

            child = next(
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.parent is not None
            )
            root_control = harness.store.get_run_control(run_id=root.id, index=0)
            child_control = harness.store.get_run_control(run_id=child.id, index=0)
            assert root_control is not None
            assert child_control is not None
            assert isinstance(root_control.payload, RunControlPayload)
            assert isinstance(child_control.payload, RunControlPayload)
            assert [
                invocation.name
                for invocation in root_control.payload.prompt_invocations
            ] == ["bracket"]
            assert [
                invocation.name
                for invocation in child_control.payload.prompt_invocations
            ] == ["bracket"]
            assert root_control.payload.prompt_invocations[0].cap_ref
            assert child_control.payload.prompt_invocations[0].cap_ref
            assert harness.adapter.invocations[0].call.messages == [
                Message.user("[hello]")
            ]

    asyncio.run(scenario())


def test_discarded_step_keeps_output_without_updating_locals(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic replace(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow retained(_: Text) -> Text:
  let run replace
""",
        responses=[ModelCallResult(message=Message.assistant("temporary"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="retained",
                    primary=resolve_input_parts("original"),
                )
            )

            assert root.status == "succeeded"
            assert harness.store.run_output_text(run_id=root.id) == "original"
            step = harness.store.list_steps(run_id=root.id)[0]
            assert step.output is not None
            assert step.output.name is None
            assert harness.store.resolve_value(step.output.value) == "temporary"

    asyncio.run(scenario())


def test_retry_reuses_committed_flow_prefix_and_keeps_the_root_run(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow staged(_: Part[]) -> Part[]:
  let note:
    committed
  run worker
""",
        responses=[
            RuntimeError("temporary model failure"),
            ModelCallResult(message=Message.assistant("recovered")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            failed = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="staged",
                    primary=resolve_input_parts("hello"),
                )
            )
            assert failed.status == "failed"
            before = harness.store.list_steps(run_id=failed.id)
            assert [(step.path.index, step.status) for step in before] == [
                (0, "succeeded"),
                (1, "failed"),
            ]
            assert before[1].input == (
                Pointer.control(failed.id, 0, "payload", "locals", 0, "value"),
            )
            previous_child = next(
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.parent == before[1].path
            )
            environment = harness.setup.environment
            assert environment is not None
            mismatched_setup = replace(
                harness.setup,
                environment=replace(
                    environment,
                    sandbox="docker:python:3.13-slim",
                    container=True,
                ),
            )
            with pytest.raises(
                ValueError,
                match="does not match original sandbox.*use rerun",
            ):
                harness.executor.retry(
                    failed.id,
                    setup=mismatched_setup,
                    state=harness.state,
                )
            assert len(harness.store.list_run_controls(run_id=failed.id)) == 1

            retried = await harness.executor.retry(
                failed.id,
                setup=harness.setup,
                state=harness.state,
            )

            assert retried.id == failed.id
            assert retried.status == "succeeded"
            assert harness.store.run_output(run_id=retried.id) == (
                TextPart("recovered"),
            )
            active = harness.store.list_steps(run_id=retried.id)
            assert [(step.path.index, step.status) for step in active] == [
                (0, "succeeded"),
                (1, "succeeded"),
            ]
            assert active[0].output is not None
            assert isinstance(active[0].output.value, Array)
            assert tuple(active[0].output.value) == (TextPart("committed"),)
            assert (
                harness.store.list_steps(
                    run_id=retried.id,
                    include_ejected=True,
                )
                == active
            )
            retry = harness.store.list_run_controls(run_id=retried.id)[-1]
            run_control = harness.store.get_run_control(run_id=retried.id, index=0)
            assert run_control is not None
            assert retry.kind == "retry"
            assert isinstance(retry.payload, RetryControlPayload)
            assert isinstance(run_control.payload, RunControlPayload)
            assert retry.payload.retry_from == before[1].path
            assert retry.payload.runnable == run_control.payload.runnable
            assert retry.payload.model == run_control.payload.model
            assert retry.payload.limits == run_control.payload.limits
            assert retry.payload.locals == run_control.payload.locals
            assert retry.payload.resources == run_control.payload.resources
            assert retry.payload.sandbox == run_control.payload.sandbox == "host"
            runs = harness.store.list_runs(
                thread_id=thread,
                limit=None,
                include_ejected=True,
            )
            assert harness.store.get_run(run_id=previous_child.id) is None
            replacement_child = next(
                run for run in runs if run.parent == before[1].path
            )
            assert replacement_child.id != previous_child.id
            assert len(harness.adapter.invocations) == 2

    asyncio.run(scenario())


def test_retry_succeeded_flow_replays_call_before_trailing_value(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow staged(_: Part[]) -> Part[]:
  run worker
  let note:
    committed
""",
        responses=[
            ModelCallResult(message=Message.assistant("first")),
            ModelCallResult(message=Message.assistant("second")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="staged",
                    primary=resolve_input_parts("hello"),
                )
            )
            original = harness.store.list_steps(run_id=run.id)
            assert [(step.kind, step.status) for step in original] == [
                ("run", "succeeded"),
                ("value", "succeeded"),
            ]

            retried = await harness.executor.retry(
                run.id,
                setup=harness.setup,
                state=harness.state,
            )

            assert retried.status == "succeeded"
            assert len(harness.adapter.invocations) == 2
            retry = harness.store.list_run_controls(run_id=run.id)[-1]
            assert retry.kind == "retry"
            assert isinstance(retry.payload, RetryControlPayload)
            assert retry.payload.retry_from == original[0].path
            current = harness.store.list_steps(
                run_id=run.id,
                include_ejected=True,
            )
            assert [(step.path.index, step.kind) for step in current] == [
                (0, "run"),
                (1, "value"),
            ]
            assert all(step.ejected_by is None for step in current)

    asyncio.run(scenario())


def test_rerun_reuses_source_invocation_in_a_new_root_run(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic reply(_: Part[], tone: Text, tags: Text[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: Reply to {{_}} in {{tone}} with {{tags}}.
""",
        responses=[
            ModelCallResult(message=Message.assistant("first")),
            ModelCallResult(message=Message.assistant("second")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            source = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=resolve_input_parts("hello"),
                    named={"tone": "brief", "tags": ("one", "two")},
                )
            )
            source_control = harness.store.get_run_control(
                run_id=source.id,
                index=0,
            )
            assert source_control is not None
            environment = harness.setup.environment
            assert environment is not None
            docker_setup = replace(
                harness.setup,
                environment=replace(
                    environment,
                    sandbox="docker:python:3.13-slim",
                    container=True,
                ),
            )
            rerun = await harness.executor.rerun(
                source.id,
                setup=docker_setup,
                state=harness.state,
            )

            assert rerun.id != source.id
            assert rerun.thread == source.thread
            assert rerun.status == "succeeded"
            assert harness.store.run_output(run_id=rerun.id) == (TextPart("second"),)
            stored_source = harness.store.get_run(run_id=source.id)
            assert stored_source is not None
            assert stored_source.ejected_by is None
            visible = harness.store.list_runs(thread_id=thread, limit=None)
            assert [run.id for run in visible] == [rerun.id, source.id]
            rerun_control = harness.store.get_run_control(run_id=rerun.id, index=0)
            assert rerun_control is not None
            assert rerun_control.kind == "rerun"
            assert isinstance(rerun_control.payload, RerunControlPayload)
            assert isinstance(source_control.payload, RunControlPayload)
            assert source_control.payload.sandbox == "host"
            assert rerun_control.payload.sandbox == "docker:python:3.13-slim"
            assert rerun_control.payload.rerun_from == source.id
            assert rerun_control.payload.runnable == source_control.payload.runnable
            assert rerun_control.payload.model == source_control.payload.model
            assert rerun_control.payload.limits == source_control.payload.limits
            assert rerun_control.payload.locals == source_control.payload.locals
            assert rerun_control.payload.resources == source_control.payload.resources
            projected = RunHistory(harness.store).get_thread(thread)
            assert projected is not None
            assert [run.id for run in projected.runs] == [source.id, rerun.id]
            payload = projected.runs[1].controls[0].payload
            assert isinstance(payload, RerunControlPayload)
            assert payload.rerun_from == source.id
            assert [call.call.messages for call in harness.adapter.invocations] == [
                [Message.user('Reply to hello in brief with ["one","two"].')],
                [Message.user('Reply to hello in brief with ["one","two"].')],
            ]

    asyncio.run(scenario())


def test_repeated_retry_reuses_the_trimmed_step_paths(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow staged(_: Part[]) -> Part[]:
  let note:
    committed
  run worker
""",
        responses=[
            RuntimeError("first failure"),
            RuntimeError("second failure"),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="staged",
                    primary=resolve_input_parts("hello"),
                )
            )
            assert run.status == "failed"
            run = await harness.executor.retry(
                run.id,
                setup=harness.setup,
                state=harness.state,
            )
            assert run.status == "failed"
            run = await harness.executor.retry(
                run.id,
                setup=harness.setup,
                state=harness.state,
            )

            assert run.status == "succeeded"
            assert [
                step.path.index for step in harness.store.list_steps(run_id=run.id)
            ] == [0, 1]
            current = harness.store.list_steps(
                run_id=run.id,
                include_ejected=True,
            )
            assert [step.path.index for step in current] == [0, 1]
            assert all(step.ejected_by is None for step in current)

    asyncio.run(scenario())


def test_retry_restores_effective_model_usage_before_enforcing_new_limits(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow twice(_: Part[]) -> Part[]:
  run worker
  run worker
""",
        responses=[
            ModelCallResult(
                message=Message.assistant("first"),
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
            RuntimeError("temporary failure"),
            ModelCallResult(
                message=Message.assistant("second"),
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="twice",
                    primary=resolve_input_parts("hello"),
                )
            )
            assert run.status == "failed"

            run = await harness.executor.retry(
                run.id,
                setup=harness.setup,
                state=harness.state,
                limits=RunLimits(tokens=2),
            )

            assert run.status == "failed"
            assert run.error == Pointer.step(StepPath(run.id, (1,)), "error")
            retry = harness.store.list_run_controls(run_id=run.id)[-1]
            assert isinstance(retry.payload, RetryControlPayload)
            assert retry.payload.limits == RunLimits(tokens=2)

    asyncio.run(scenario())


def test_parallel_flow_failure_terminates_every_started_child(
    tmp_path: Path,
) -> None:
    gates = [AsyncGate() for _ in range(3)]
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow parallel(_: Part[]):
  storm 3 worker par 3
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant(f"item {index}")),
                gate=gate,
            )
            for index, gate in enumerate(gates)
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="parallel",
                    primary=resolve_input_parts("work"),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(
                asyncio.gather(*(gate.wait_until_entered() for gate in gates)),
                timeout=1,
            )
            gates[0].fail(RuntimeError("worker failed"))
            root = await asyncio.wait_for(handle, timeout=2)

            runs = harness.store.list_runs(thread_id=thread, limit=None)
            children = [run for run in runs if run.parent is not None]
            assert root.status == "failed"
            assert len(children) == 3
            assert sorted(run.status for run in children) == [
                "canceled",
                "canceled",
                "failed",
            ]
            failed_child = next(run for run in children if run.status == "failed")
            leaf_path = StepPath(failed_child.id, (0,))
            root_path = StepPath(root.id, (0,))
            root_step = harness.store.list_steps(run_id=root.id)[0]
            leaf_step = harness.store.list_steps(run_id=failed_child.id)[0]
            assert root.error == Pointer.step(root_path, "error")
            boundary_error = "parallel step stopped because lane 0 (#0) failed"
            assert root_step.error == boundary_error
            assert failed_child.error == Pointer.step(leaf_path, "error")
            assert leaf_step.error == "worker failed"
            assert isinstance(root.error, Pointer)
            assert harness.store.resolve_error(root.error) == boundary_error
            detail = RunHistory(harness.store).get_run(root.id)
            assert detail is not None
            assert detail.summary == boundary_error
            assert all(
                step.status != "running"
                for run in runs
                for step in harness.store.list_steps(run_id=run.id)
            )
            assert [
                (step.kind, step.status)
                for step in harness.store.list_steps(run_id=root.id)
            ] == [("par", "failed")]
            assert_run_event_integrity(tracer.events)
            assert sum(isinstance(event, RunBegin) for event in tracer.events) == 4
            assert sum(isinstance(event, RunEnd) for event in tracer.events) == 4

    asyncio.run(scenario())


def test_runtime_failure_outside_a_step_is_recorded_on_the_run(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow fail(_: Part[]) -> Number:
  let note:
    captured
""",
        responses=[],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="fail",
                    primary=resolve_input_parts("not a number"),
                ),
                tracer=tracer,
            )

            assert record.status == "failed"
            assert isinstance(record.error, str)
            assert record.error.startswith("output is not valid Number")
            steps = harness.store.list_steps(run_id=record.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("value", "succeeded"),
            ]
            assert_run_event_integrity(tracer.events)
            assert event_labels(tracer.events) == [
                f"run_begin:{record.id}",
                f"step_begin:{record.id}.0:value",
                f"step_end:{record.id}.0:value:succeeded",
                f"run_end:{record.id}:failed",
            ]

    asyncio.run(scenario())


def test_home_flow_module_executes_with_local_types_and_helpers(tmp_path: Path) -> None:
    home = tmp_path / "agents" / "alice"
    flows = home / "flows"
    flows.mkdir(parents=True)
    agent_source = "agent alice\n"
    (home / "agent.too").write_text(agent_source, encoding="utf-8")
    (flows / "research.too").write_text(
        """
struct Brief:
  title: Text

agic echo(brief: Brief) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{brief.title}}

flow research(brief: Brief) -> Text:
  run echo
""".lstrip(),
        encoding="utf-8",
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="",
        responses=[ModelCallResult(message=Message.assistant("completed"))],
    )
    harness.state = prepare_agent_state(
        harness.setup.layout,
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:research",
                    named={"brief": {"title": "Module-local input"}},
                )
            )

            assert root.status == "succeeded", root.error
            assert harness.store.run_output_text(run_id=root.id) == "completed"
            accepted = harness.store.get_run_control(run_id=root.id, index=0)
            assert accepted is not None
            assert isinstance(accepted.payload, RunControlPayload)
            assert accepted.payload.runnable == "flow:research"
            child = next(
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.parent is not None
            )
            child_run_control = harness.store.get_run_control(run_id=child.id, index=0)
            assert child_run_control is not None
            assert isinstance(child_run_control.payload, RunControlPayload)
            assert child_run_control.payload.runnable == "agic:echo"
            assert "Module-Local Input" in message_text(
                harness.adapter.invocations[0].call.messages[0].parts
            )

    asyncio.run(scenario())


def test_parent_step_points_to_child_runtime_error_without_copying_it(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow child(_: Part[]) -> Number:
  let note:
    captured

flow parent(_: Part[]) -> Number:
  run child
""",
        responses=[],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="parent",
                    primary=resolve_input_parts("not a number"),
                )
            )

            child = next(
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.parent is not None
            )
            parent_step = harness.store.list_steps(run_id=root.id)[0]
            assert isinstance(child.error, str)
            assert child.error.startswith("output is not valid Number")
            assert isinstance(parent_step.error, Pointer)
            assert parent_step.error == Pointer.run(child.id, "error")
            assert harness.store.resolve_error(parent_step.error) == child.error

    asyncio.run(scenario())


def test_scatter_then_map_preserves_item_order_and_types(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

agic upper(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow mapped(_: Text) -> Text[]:
  scatter 2 split
  map upper par 2
""",
        responses=[
            ModelCallResult(message=Message.assistant('["one","two"]')),
            ModelCallResult(message=Message.assistant("ONE")),
            ModelCallResult(message=Message.assistant("TWO")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="mapped",
                    primary=resolve_input_parts("split this"),
                )
            )
            assert root.status == "succeeded"
            assert _output_value(harness, root.id) == ["ONE", "TWO"]
            assert _root_step_kinds(harness, root.id) == ["run", "par"]
            assert [
                invocation.call.messages[-1]
                for invocation in harness.adapter.invocations[1:]
            ] == [Message.user("one"), Message.user("two")]
            children = [
                run
                for run in harness.store.list_runs(
                    thread_id=thread,
                    limit=None,
                )
                if run.parent == StepPath.parse(f"{root.id}.1")
            ]
            assert sorted(
                run.occur.item.index
                for run in children
                if run.occur is not None and run.occur.item is not None
            ) == [
                0,
                1,
            ]

    asyncio.run(scenario())


def test_deep_search_example_uses_explicit_flow_reshaping(
    tmp_path: Path,
) -> None:
    source = (Path(__file__).parents[3] / "examples" / "deep_search.too").read_text(
        encoding="utf-8"
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[
            ModelCallResult(
                message=Message.assistant(
                    '["query 1","query 2","query 3","query 4","query 5","query 6"]'
                )
            ),
            *[
                ModelCallResult(message=Message.assistant(f"evidence {index}"))
                for index in range(6)
            ],
            *[
                ModelCallResult(
                    message=Message.assistant("true" if index % 2 == 0 else "false")
                )
                for index in range(6)
            ],
            *[
                ModelCallResult(message=Message.assistant(score))
                for score in ("3", "1", "2")
            ],
            *[
                ModelCallResult(message=Message.assistant(f"finding {index}"))
                for index in range(3)
            ],
            ModelCallResult(message=Message.assistant("report")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="research",
                    primary=resolve_input_parts("agent framework/sdk"),
                )
            )

            assert root.status == "succeeded", root.error
            assert harness.store.run_output_text(run_id=root.id) == "report"
            assert _root_step_kinds(harness, root.id) == [
                "value",
                "run",
                "par",
                "par",
                "par",
                "par",
                "run",
            ]
            root_steps = [
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.parent is None
            ]
            assert root_steps[3].noted == CollectionStepNoted(6, 3)
            assert root_steps[4].noted == CollectionStepNoted(3, 3)
            assert len(harness.adapter.invocations) == 20
            predicate_messages = [
                message_text(invocation.call.messages[-1].parts)
                for invocation in harness.adapter.invocations[7:13]
            ]
            assert all(
                len(invocation.call.messages) == 1
                for invocation in harness.adapter.invocations[7:13]
            )
            assert all(
                "Research question:\nagent framework/sdk" in message
                for message in predicate_messages
            )
            final_message = harness.adapter.invocations[-1].call.messages[-1]
            assert any(
                isinstance(part, TextPart)
                and "Research question:\nagent framework/sdk" in part.text
                and "Findings:" in part.text
                for part in final_message.parts
            )

    asyncio.run(scenario())


def test_inline_rank_scorer_has_no_recalled_history_or_tools(
    tmp_path: Path,
) -> None:
    tool = RecordingTool("test__side_effect", output={"ok": True})
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic remember(_: Text) -> Text:
  recall = none
  context: none
  user: {{_}}

agic split(_: Text) -> Text[]:
  recall = none
  context: none
  user: {{_}}

flow select(_: Text) -> Text[]:
  scatter 1 split
  rank top 1: Return a numeric relevance score from 0 to 10.
""",
        tools={tool.name: tool},
        responses=[
            ModelCallResult(message=Message.assistant("history marker")),
            ModelCallResult(message=Message.assistant('["candidate"]')),
            ModelCallResult(message=Message.assistant("10")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            remembered = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="remember",
                    primary=resolve_input_parts("remember this"),
                )
            )
            selected = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="select",
                    primary=resolve_input_parts("candidate"),
                )
            )

            assert remembered.status == "succeeded"
            assert selected.status == "succeeded", selected.error
            assert _output_value(harness, selected.id) == ["candidate"]
            score_call = harness.adapter.invocations[-1].call
            assert score_call.tools == ()
            assert all(
                "history marker" not in message_text(message.parts)
                for message in score_call.messages
            )
            assert tool.calls == []

    asyncio.run(scenario())


def test_storm_honors_parallel_limit_and_preserves_result_order(
    tmp_path: Path,
) -> None:
    gates = [AsyncGate() for _ in range(3)]
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow fanout(_: Text) -> Text[]:
  storm 3 worker par 2
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant(f"item {index}")),
                gate=gate,
            )
            for index, gate in enumerate(gates)
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="fanout",
                    primary=resolve_input_parts("work"),
                )
            )
            await asyncio.wait_for(
                asyncio.gather(
                    gates[0].wait_until_entered(),
                    gates[1].wait_until_entered(),
                ),
                timeout=1,
            )
            assert not gates[2].entered

            gates[1].release()
            await asyncio.wait_for(gates[2].wait_until_entered(), timeout=1)
            gates[2].release()
            gates[0].release()
            root = await asyncio.wait_for(handle, timeout=2)

            assert root.status == "succeeded"
            assert _output_value(harness, root.id) == [
                "item 0",
                "item 1",
                "item 2",
            ]
            assert _root_step_kinds(harness, root.id) == ["par"]

    asyncio.run(scenario())


def test_scatter_then_gather_reshapes_the_list_to_one_item(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

agic merge(_: Text[]) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow summary(_: Text) -> Text:
  scatter 3 split
  gather merge
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b","c"]')),
            ModelCallResult(message=Message.assistant("a+b+c")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="summary",
                    primary=resolve_input_parts("summarize"),
                )
            )

            assert root.status == "succeeded"
            assert harness.store.run_output_text(run_id=root.id) == "a+b+c"
            assert _root_step_kinds(harness, root.id) == ["run", "run"]
            assert harness.adapter.invocations[-1].call.messages[-1] == (
                Message.user('["a","b","c"]')
            )

    asyncio.run(scenario())


def test_scatter_then_settle_carries_the_accumulator_sequentially(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

agic fold(_: Part[], item: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}{{item}}

flow folded(_: Text) -> Text:
  scatter 3 split
  settle fold
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b","c"]')),
            ModelCallResult(message=Message.assistant("a")),
            ModelCallResult(message=Message.assistant("ab")),
            ModelCallResult(message=Message.assistant("abc")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="folded",
                    primary=resolve_input_parts("fold"),
                )
            )
            assert root.status == "succeeded"
            assert harness.store.run_output_text(run_id=root.id) == "abc"
            assert _root_step_kinds(harness, root.id) == ["run", "loop"]
            assert [
                invocation.call.messages[-1]
                for invocation in harness.adapter.invocations[1:]
            ] == [
                Message.user("a"),
                Message.user("ab"),
                Message.user("abc"),
            ]

    asyncio.run(scenario())


def test_inline_settle_receives_empty_accumulator_and_zero_based_items(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow folded(_: Text) -> Text:
  scatter 3 split
  settle -> Text:
    {{_}}{{item}}
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b","c"]')),
            ModelCallResult(message=Message.assistant("a")),
            ModelCallResult(message=Message.assistant("ab")),
            ModelCallResult(message=Message.assistant("abc")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="folded",
                    primary=resolve_input_parts("fold"),
                ),
                tracer=tracer,
            )
            assert root.status == "succeeded"
            assert harness.store.run_output_text(run_id=root.id) == "abc"
            assert [
                message_text(invocation.call.messages[-1].parts).rsplit("\n", 1)[-1]
                for invocation in harness.adapter.invocations[1:]
            ] == [
                "a",
                "ab",
                "abc",
            ]
            occurrences = [
                event.occurrence
                for event in tracer.events
                if isinstance(event, RunBegin)
                and event.parent is not None
                and event.occurrence is not None
                and event.occurrence.item is not None
            ]
            assert occurrences == [
                Occurrence(
                    item=OccurrencePosition(index=0, count=3),
                    iteration=IterationOccurrence(index=0, count=3, phase="body"),
                ),
                Occurrence(
                    item=OccurrencePosition(index=1, count=3),
                    iteration=IterationOccurrence(index=1, count=3, phase="body"),
                ),
                Occurrence(
                    item=OccurrencePosition(index=2, count=3),
                    iteration=IterationOccurrence(index=2, count=3, phase="body"),
                ),
            ]
            loop = next(
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.kind == "loop"
            )
            assert loop.noted == LoopStepNoted(
                iterations=3,
                termination="exhausted",
                total=3,
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("keep first 3", ["a", "b", "c"]),
        ("keep last 2", ["c", "d"]),
        ("drop first 3", ["d"]),
        ("drop last 2", ["a", "b"]),
    ],
)
def test_positional_keep_and_drop(
    tmp_path: Path,
    statement: str,
    expected: list[str],
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{{{_}}}}

flow selected(_: Text) -> Text[]:
  scatter 4 split
  {statement}
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b","c","d"]')),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="selected",
                    primary=resolve_input_parts("select"),
                )
            )

            assert root.status == "succeeded"
            assert _output_value(harness, root.id) == expected
            assert _root_step_kinds(harness, root.id) == ["run", "value"]
            selected = [
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.parent is None
            ][-1]
            assert selected.noted == CollectionStepNoted(4, len(expected))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("keep relevant par 2", ["a", "c"]),
        ("drop relevant par 2", ["b"]),
    ],
)
def test_predicate_keep_and_drop(
    tmp_path: Path,
    statement: str,
    expected: list[str],
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{{{_}}}}

agic relevant(_: Text) -> Boolean:
  recall = none
  context: none
  instruct: none
  user: {{{{_}}}}

flow selected(_: Text) -> Text[]:
  scatter 3 split
  {statement}
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b","c"]')),
            ModelCallResult(message=Message.assistant("true")),
            ModelCallResult(message=Message.assistant("false")),
            ModelCallResult(message=Message.assistant("true")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="selected",
                    primary=resolve_input_parts("select"),
                )
            )

            assert root.status == "succeeded"
            assert _output_value(harness, root.id) == expected
            assert _root_step_kinds(harness, root.id) == ["run", "par"]
            selected = [
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.parent is None
            ][-1]
            assert selected.noted == CollectionStepNoted(3, len(expected))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ("top 3", ["a", "b", "d"]),
        ("bottom 2", ["d", "c"]),
    ],
)
def test_rank_is_stable_and_applies_top_or_bottom_selection(
    tmp_path: Path,
    selection: str,
    expected: list[str],
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{{{_}}}}

agic score(_: Text) -> Number:
  recall = none
  context: none
  instruct: none
  user: {{{{_}}}}

flow ranked(_: Text) -> Text[]:
  scatter 4 split
  rank score {selection} par 2
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b","c","d"]')),
            ModelCallResult(message=Message.assistant("3")),
            ModelCallResult(message=Message.assistant("3")),
            ModelCallResult(message=Message.assistant("1")),
            ModelCallResult(message=Message.assistant("2")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="ranked",
                    primary=resolve_input_parts("rank"),
                )
            )

            assert root.status == "succeeded"
            assert _output_value(harness, root.id) == expected
            assert _root_step_kinds(harness, root.id) == ["run", "par"]
            ranked = [
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.parent is None
            ][-1]
            assert ranked.noted == CollectionStepNoted(4, len(expected))

    asyncio.run(scenario())


def test_repeat_count_chains_each_iteration_output(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic echo(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow repeated(_: Text) -> Text:
  repeat 3:
    run echo
""",
        responses=[
            ModelCallResult(message=Message.assistant("one")),
            ModelCallResult(message=Message.assistant("two")),
            ModelCallResult(message=Message.assistant("three")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="repeated",
                    primary=resolve_input_parts("zero"),
                )
            )

            assert root.status == "succeeded"
            assert harness.store.run_output_text(run_id=root.id) == "three"
            assert _root_step_kinds(harness, root.id) == ["loop"]
            loop = harness.store.list_steps(run_id=root.id)[0]
            assert loop.noted == LoopStepNoted(
                iterations=3,
                termination="exhausted",
                total=3,
            )
            assert [
                invocation.call.messages[-1]
                for invocation in harness.adapter.invocations
            ] == [
                Message.user("zero"),
                Message.user("one"),
                Message.user("two"),
            ]

    asyncio.run(scenario())


def test_retry_restores_locals_written_inside_a_committed_repeat(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic echo(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow repeated(_: Text) -> Text:
  repeat 2:
    run echo
    until: Return false.
  run echo
""",
        responses=[
            ModelCallResult(message=Message.assistant("one")),
            ModelCallResult(message=Message.assistant("false")),
            ModelCallResult(message=Message.assistant("two")),
            ModelCallResult(message=Message.assistant("false")),
            RuntimeError("temporary failure"),
            ModelCallResult(message=Message.assistant("recovered")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="repeated",
                    primary=resolve_input_parts("zero"),
                )
            )
            assert run.status == "failed"

            run = await harness.executor.retry(
                run.id,
                setup=harness.setup,
                state=harness.state,
            )

            assert run.status == "succeeded"
            assert harness.store.run_output_text(run_id=run.id) == "recovered"
            assert [
                harness.adapter.invocations[index].call.messages[-1]
                for index in (0, 2, 4, 5)
            ] == [
                Message.user("zero"),
                Message.user("one"),
                Message.user("two"),
                Message.user("two"),
            ]

    asyncio.run(scenario())


def test_run_limits_reset_agic_calls_but_share_root_token_usage(
    tmp_path: Path,
) -> None:
    limits = RunLimits(agic_model_calls=1, tokens=8)
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic echo(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow repeated(_: Text) -> Text:
  repeat 2:
    run echo
""",
        responses=[
            ModelCallResult(
                message=Message.assistant(value),
                usage=ModelUsage(input_tokens=3, output_tokens=2),
            )
            for value in ("one", "two")
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="repeated",
                    primary=resolve_input_parts("hello"),
                    limits=limits,
                ),
            )

            children = [
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.parent is not None
            ]
            assert root.status == "failed"
            assert root.error == Pointer.step(StepPath(root.id, (0,)), "error")
            assert len(harness.adapter.invocations) == 2
            assert sorted(run.status for run in children) == ["failed", "succeeded"]
            root_run_control = harness.store.get_run_control(run_id=root.id, index=0)
            assert root_run_control is not None
            assert isinstance(root_run_control.payload, RunControlPayload)
            assert root_run_control.payload.limits == limits
            for child in children:
                child_run_control = harness.store.get_run_control(
                    run_id=child.id,
                    index=0,
                )
                assert child_run_control is not None
                assert isinstance(child_run_control.payload, RunControlPayload)

    asyncio.run(scenario())


def test_repeat_until_stops_before_the_count_limit(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic echo(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow repeated(_: Text) -> Text:
  repeat 5:
    run echo
    until:
      Return true when complete.
""",
        responses=[
            ModelCallResult(message=Message.assistant("one")),
            ModelCallResult(message=Message.assistant("false")),
            ModelCallResult(message=Message.assistant("two")),
            ModelCallResult(message=Message.assistant("true")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="repeated",
                    primary=resolve_input_parts("zero"),
                )
            )

            assert root.status == "succeeded"
            assert harness.store.run_output_text(run_id=root.id) == "two"
            assert _root_step_kinds(harness, root.id) == ["loop"]
            assert len(harness.adapter.invocations) == 4
            loop = next(
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.parent is None
            )
            assert loop.output is None
            assert loop.noted == LoopStepNoted(
                iterations=2,
                termination="satisfied",
                total=5,
            )
            until_runs = [
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.occur is not None
                and run.occur.iteration is not None
                and run.occur.iteration.phase == "until"
            ]
            assert len(until_runs) == 2
            assert all(
                run.output is not None and run.output.name is None for run in until_runs
            )
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("statement", "step_kind", "operation"),
    [
        ("map echo", "par", "map"),
        ("gather echo", "run", "gather"),
        ("settle echo", "loop", "settle"),
    ],
)
def test_list_statements_fail_inside_their_own_step_boundary(
    tmp_path: Path,
    statement: str,
    step_kind: str,
    operation: str,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
agic echo(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{{{_}}}}

flow invalid(_: Text) -> Text:
  {statement}
""",
        responses=[],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="invalid",
                    primary=resolve_input_parts("not a list"),
                ),
                tracer=tracer,
            )

            error = f"{operation} requires current shape list, got item"
            assert root.status == "failed"
            assert root.error == Pointer.step(StepPath(root.id, (0,)), "error")
            steps = [
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.parent is None
            ]
            assert [(step.kind, step.status, step.error) for step in steps] == [
                (step_kind, "failed", error)
            ]
            assert harness.adapter.invocations == []
            assert_run_event_integrity(tracer.events)
            assert event_labels(tracer.events) == [
                f"run_begin:{root.id}",
                f"step_begin:{root.id}.0:{step_kind}",
                f"step_end:{root.id}.0:{step_kind}:failed",
                f"run_end:{root.id}:failed",
            ]

    asyncio.run(scenario())


def test_scatter_uses_the_returned_list_length_instead_of_authored_count(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic split(_: Text) -> Text[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow scattered(_: Text) -> Text[]:
  scatter 3 split
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b"]')),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="scattered",
                    primary=resolve_input_parts("split"),
                )
            )

            assert root.status == "succeeded"
            assert _output_value(harness, root.id) == ["a", "b"]
            root_steps = [
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.parent is None
            ]
            assert [(step.kind, step.status) for step in root_steps] == [
                ("run", "succeeded")
            ]
            assert root_steps[0].noted is None
            child = next(
                run
                for run in harness.store.list_runs(
                    thread_id=thread,
                    limit=None,
                )
                if run.parent is not None
            )
            assert child.status == "succeeded"

    asyncio.run(scenario())


def test_inline_scatter_requests_and_returns_an_array_result(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow scattered(_: Text) -> Text[]:
  let source:
    {{_}}

  scatter 3 -> Text:
    Return distinct pieces of this source:
    {{source}}
""",
        responses=[
            ModelCallResult(message=Message.assistant('["a","b"]')),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="scattered",
                    primary=resolve_input_parts("split"),
                )
            )

            assert root.status == "succeeded"
            assert _output_value(harness, root.id) == ["a", "b"]
            assert harness.adapter.invocations[0].call.output_schema == {
                "items": {"type": "string"},
                "type": "array",
            }
            assert "Return distinct pieces of this source:\nsplit" in message_text(
                harness.adapter.invocations[0].call.messages[-1].parts
            )

    asyncio.run(scenario())


def test_recursive_run_forwards_named_arguments_and_primary_local(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic combine(_: Text, suffix: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}{{suffix}}

flow relay(_: Text, suffix: Text) -> Text:
  run combine
""",
        responses=[
            ModelCallResult(message=Message.assistant("hello!")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="relay",
                    primary=resolve_input_parts("hello"),
                    named={"suffix": "!"},
                )
            )

            assert root.status == "succeeded"
            assert harness.adapter.invocations[0].call.messages == [
                Message.user("hello!")
            ]
            child = next(
                run
                for run in harness.store.list_runs(
                    thread_id=thread,
                    limit=None,
                )
                if run.parent is not None
            )
            run_control = harness.store.get_run_control(run_id=child.id, index=0)
            assert run_control is not None
            assert isinstance(run_control.payload, RunControlPayload)
            suffix = next(
                local for local in run_control.payload.locals if local.name == "suffix"
            )
            assert suffix.value == TypedPointer(
                "Text",
                Pointer.control(root.id, 0, "payload", "locals", 1, "value"),
            )
            parent_step = harness.store.list_steps(run_id=root.id)[0]
            assert parent_step.input == (
                Pointer.control(root.id, 0, "payload", "locals", 0, "value"),
                Pointer.control(root.id, 0, "payload", "locals", 1, "value"),
            )
            assert harness.store.run_output_text(run_id=root.id) == "hello!"

    asyncio.run(scenario())


def test_recursive_run_persists_the_coerced_child_primary_value(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic number(_: Number) -> Number:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow relay(_: Text) -> Number:
  run number
""",
        responses=[ModelCallResult(message=Message.assistant("7"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="relay",
                    primary=resolve_input_parts("42"),
                )
            )

            child = next(
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.parent is not None
            )
            run_control = harness.store.get_run_control(run_id=child.id, index=0)
            assert run_control is not None
            assert isinstance(run_control.payload, RunControlPayload)
            assert run_control.payload.locals == (Local.typed("Number", 42, "_", 0),)
            assert (
                harness.store.resolve_local(run_control.payload.locals[0]).value == 42
            )
            assert harness.store.run_output_text(run_id=root.id) == "7"

    asyncio.run(scenario())


def test_flow_output_coercion_drops_incompatible_step_provenance(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic text_number(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow number(_: Text) -> Number:
  run text_number
""",
        responses=[ModelCallResult(message=Message.assistant("7"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="number",
                    primary=resolve_input_parts("input"),
                )
            )

            assert root.output == Local.typed("Number", 7, "_", 0)
            assert root.output is not None
            assert harness.store.resolve_local(root.output).value == 7

    asyncio.run(scenario())
