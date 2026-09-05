"""Agic-owned runtime reload and public runnable call scenarios."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.support.execution_assertions import assert_run_event_integrity
from tests.support.execution_harness import (
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
)
from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.common.layout import AgentLayout
from toolang.execution.executor.steps.tool import invoke_tool_call
from toolang.execution.records import ExecuteControlPayload, RunControlPayload
from toolang.execution.types import (
    ErrorMessage,
    ErrorRef,
    FieldRef,
    RunRef,
    ThreadPrefix,
    TypedRef,
)
from toolang.lang.ast import RunStmt
from toolang.lang.input import resolve_input_parts
from toolang.state.prepare import prepare_agent_state
from toolang.state.watcher import StateWatcher


def test_agic_dynamic_run_is_one_run_step_and_one_child(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text) -> Text:
  recall = none
  hands = agic:child
  context: none
  instruct: none
  user: {{_}}

agic child(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: Child {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call-run",
                        call_id="provider-run",
                        name="_too__run",
                        input={
                            "runnable": "agic:child",
                            "input": {"_": "topic"},
                        },
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("child output")),
            ModelCallResult(message=Message.assistant("parent output")),
        ),
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                ),
                tracer=tracer,
            )

            assert root.status == "succeeded", root.error
            root_steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in root_steps] == ["model", "run", "model"]
            dynamic = root_steps[1]
            assert isinstance(dynamic.given, RunStmt)
            assert dynamic.given.runnable == "agic:child"
            assert dynamic.input == (
                FieldRef.from_path(root_steps[0].ref, "output", "value", 0),
            )
            dynamic_output = dynamic.output
            assert dynamic_output is not None
            assert isinstance(dynamic_output.value, TypedRef)
            assert harness.store.resolve_value(dynamic_output.value) == "child output"
            children = [
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.parent is not None
            ]
            assert len(children) == 1
            assert children[0].parent == dynamic.ref
            child_control = harness.store.get_run_control(
                run_id=str(children[0].control.target),
                index=children[0].control.index,
            )
            assert child_control is not None
            assert isinstance(child_control.payload, RunControlPayload)
            assert child_control.payload.runnable == "agent$agic:child"
            assert all(step.kind != "tool" for step in root_steps)
            followup = harness.adapter.invocations[2].call
            result = followup.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.tool_call_id == "call-run"
            assert result.output["run_id"] == children[0].id
            assert "<available-runnable-routes>" in (
                harness.adapter.invocations[0].call.instructions
            )
            assert "<available-runnable-routes>" not in (
                harness.adapter.invocations[1].call.instructions
            )
            assert "declares no hands or handoffs" in (
                harness.adapter.invocations[1].call.instructions
            )
            assert {
                tool.name for tool in harness.adapter.invocations[1].call.tools
            } == {
                "_too__execute",
                "_too__reload",
                "_too__run",
            }
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "primary",
    (
        "candidate",
        [{"type": "text", "text": "candidate"}],
    ),
)
def test_dynamic_run_decodes_part_array_wire_input(
    tmp_path: Path,
    primary: object,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text) -> Text:
  recall = none
  hands = flow:check
  context: none
  instruct: none
  user: {{_}}

agic reviewer(_: Part[]) -> Text:
  recall = none
  context: none
  instruct: none
  user: Review {{_}}

flow check(_: Part[]) -> Text:
  run reviewer
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="run-check",
                        call_id="provider-run-check",
                        name="_too__run",
                        input={
                            "runnable": "flow:check",
                            "input": {"_": primary},
                        },
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("checked")),
            ModelCallResult(message=Message.assistant("done")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            runs = harness.store.list_run_tree(root_run_id=root.id)
            assert len(runs) == 3
            reviewer_call = harness.adapter.invocations[1].call
            assert reviewer_call.messages[-1] == Message.user("Review candidate")

    asyncio.run(scenario())


def test_dynamic_run_input_failure_returns_the_expected_signature(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text) -> Text:
  recall = none
  hands = flow:check
  context: none
  instruct: none
  user: {{_}}

flow check(_: Text, threshold: Number) -> Text:
  pass
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="run-check",
                        call_id="provider-run-check",
                        name="_too__run",
                        input={
                            "runnable": "flow:check",
                            "input": {"_": "candidate"},
                        },
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("What threshold should I use?")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            result = harness.adapter.invocations[1].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.error == "missing named inputs for check: threshold"
            assert result.output == {
                "error": "missing named inputs for check: threshold",
                "code": "invalid_runnable_input",
                "runnable": "flow:check",
                "expected": {
                    "input": {"optional": False, "type": "Text"},
                    "parameters": [
                        {
                            "name": "threshold",
                            "optional": False,
                            "type": "Number",
                        }
                    ],
                    "structs": [],
                },
                "guidance": (
                    "Retry only when available context provides the required "
                    "values; otherwise respond to the user in the normal model "
                    "output with a specific question."
                ),
            }
            assert root.output is not None
            assert harness.store.resolve_value(root.output.value) == (
                "What threshold should I use?"
            )

    asyncio.run(scenario())


def test_dynamic_run_input_failure_can_be_corrected(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text) -> Text:
  recall = none
  hands = flow:check
  context: none
  instruct: none
  user: {{_}}

flow check(_: Text, threshold: Number) -> Text:
  pass
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="run-check-invalid",
                        call_id="provider-run-check-invalid",
                        name="_too__run",
                        input={
                            "runnable": "flow:check",
                            "input": {"_": "candidate"},
                        },
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="run-check-corrected",
                        call_id="provider-run-check-corrected",
                        name="_too__run",
                        input={
                            "runnable": "flow:check",
                            "input": {"_": "candidate", "threshold": 0.8},
                        },
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("checked")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            assert len(harness.store.list_run_tree(root_run_id=root.id)) == 2
            assert [
                (step.kind, step.status)
                for step in harness.store.list_steps(run_id=root.id)
            ] == [
                ("model", "succeeded"),
                ("run", "failed"),
                ("model", "succeeded"),
                ("run", "succeeded"),
                ("model", "succeeded"),
            ]

    asyncio.run(scenario())


def test_dynamic_run_rejects_the_current_agic_and_model_recovers(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text, threshold: Number) -> Text:
  recall = none
  hands = agic:parent
  context: none
  instruct: none
  user: {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="run-self",
                        call_id="provider-run-self",
                        name="_too__run",
                        input={
                            "runnable": "agic:parent",
                            "input": {"_": "again"},
                        },
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("recovered")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                    named={"threshold": 0.8},
                )
            )

            assert root.status == "succeeded", root.error
            steps = harness.store.list_steps(run_id=root.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("run", "failed"),
                ("model", "succeeded"),
            ]
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            result = harness.adapter.invocations[1].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.error == (
                "_too/run cannot call the current or an ancestor runnable: agic:parent"
            )
            assert result.output == {"error": result.error}

    asyncio.run(scenario())


def test_dynamic_run_rejects_an_ancestor_flow_and_model_recovers(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller(_: Text) -> Text:
  recall = none
  hands = flow:outer
  context: none
  instruct: none
  user: {{_}}

flow outer(_: Text) -> Text:
  run caller
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="run-ancestor",
                        call_id="provider-run-ancestor",
                        name="_too__run",
                        input={
                            "runnable": "flow:outer",
                            "input": {"_": "again"},
                        },
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("recovered")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:outer",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            runs = harness.store.list_run_tree(root_run_id=root.id)
            assert len(runs) == 2
            caller = next(run for run in runs if run.id != root.id)
            steps = harness.store.list_steps(run_id=caller.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("run", "failed"),
                ("model", "succeeded"),
            ]
            result = harness.adapter.invocations[1].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.error == (
                "_too/run cannot call the current or an ancestor runnable: flow:outer"
            )
            assert result.output == {"error": result.error}

    asyncio.run(scenario())


def test_dynamic_run_rejects_reloaded_ancestor_by_public_identity(
    tmp_path: Path,
) -> None:
    source = """
agic default(_: Text) -> Text:
  Return {{_}}
"""
    initial_flow = """
agic caller(_: Text) -> Text:
  recall = none
  hands = flow:outer
  context: none
  instruct: none
  user: {{_}}

flow -> Text:
  run caller
"""
    reloaded_flow = initial_flow.replace(
        "flow -> Text:", "flow outer(_: Text) -> Text:"
    )
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text(source, encoding="utf-8")
    flows = layout.home / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    flow_source = flows / "outer.too"
    flow_source.write_text(initial_flow, encoding="utf-8")
    initial = prepare_agent_state(layout)
    watcher = StateWatcher(layout)
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=initial,
        refresh_state=watcher.refresh_result,
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="reload-ancestor",
                        call_id="provider-reload-ancestor",
                        name="_too__reload",
                        input={},
                    ),
                    ToolCall(
                        tool_call_id="run-reloaded-ancestor",
                        call_id="provider-run-reloaded-ancestor",
                        name="_too__run",
                        input={
                            "runnable": "flow:outer",
                            "input": {"_": "again"},
                        },
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("recovered")),
        ),
    )

    async def scenario() -> None:
        await watcher.refresh()
        flow_source.write_text(reloaded_flow, encoding="utf-8")
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:outer",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            runs = harness.store.list_run_tree(root_run_id=root.id)
            assert len(runs) == 2
            caller = next(run for run in runs if run.id != root.id)
            steps = harness.store.list_steps(run_id=caller.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "succeeded"),
                ("run", "failed"),
                ("model", "succeeded"),
            ]
            result = harness.adapter.invocations[1].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.error == (
                "_too/run cannot call the current or an ancestor runnable: flow:outer"
            )

    asyncio.run(scenario())


def test_generic_tool_dispatch_rejects_executor_action_names(tmp_path: Path) -> None:
    result = asyncio.run(
        invoke_tool_call(
            run_id="run-test",
            tools={
                "_too__execute": RecordingTool("_too__execute", output={"bad": True})
            },
            services=(),
            layout=harness_layout(tmp_path),
            call=ToolCall(
                tool_call_id="execute",
                call_id="provider-execute",
                name="_too__execute",
                input={},
            ),
        )
    )

    assert result.output == {}
    assert result.error == (
        "inner runtime tool cannot use generic tool dispatch: _too__execute"
    )


def test_invalid_dynamic_run_records_failure_and_model_recovers(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text) -> Text:
  recall = none
  hands = missing
  context: none
  instruct: none
  user: {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="bad-run",
                        call_id="provider-bad-run",
                        name="_too__run",
                        input={"input": {}},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("recovered")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == ["model", "run", "model"]
            assert steps[1].status == "failed"
            assert isinstance(steps[1].given, RunStmt)
            assert steps[1].given.runnable == ""
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            result = harness.adapter.invocations[1].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.tool_call_id == "bad-run"
            assert result.error == "_too/run requires a non-empty runnable ref"

    asyncio.run(scenario())


def test_dynamic_child_failure_keeps_its_pointer_and_model_recovers(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text) -> Text:
  recall = none
  hands = agic:child
  context: none
  instruct: none
  user: {{_}}

agic child(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="failed-child",
                        call_id="provider-failed-child",
                        name="_too__run",
                        input={
                            "runnable": "agic:child",
                            "input": {"_": "topic"},
                        },
                    ),
                )
            ),
            RuntimeError("child provider failed"),
            ModelCallResult(message=Message.assistant("recovered")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == ["model", "run", "model"]
            dynamic = steps[1]
            assert dynamic.status == "failed"
            child = next(
                run
                for run in harness.store.list_run_tree(root_run_id=root.id)
                if run.parent == dynamic.ref
            )
            assert child.status == "failed"
            assert dynamic.error == ErrorRef(
                FieldRef.from_path(RunRef.parse(child.id), "error")
            )
            result = harness.adapter.invocations[2].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.tool_call_id == "failed-child"
            assert "child provider failed" in (result.error or "")

    asyncio.run(scenario())


def test_reload_records_a_tool_step_and_new_flow_runs_in_same_root(
    tmp_path: Path,
) -> None:
    source = """
agic parent(_: Text) -> Text:
  recall = none
  hands = flow:new_flow
  context: none
  instruct: none
  user: {{_}}
"""
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text(source, encoding="utf-8")
    initial = prepare_agent_state(layout)
    watcher = StateWatcher(layout)
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=initial,
        refresh_state=watcher.refresh_result,
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call-reload",
                        call_id="provider-reload",
                        name="_too__reload",
                        input={},
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call-new-flow",
                        call_id="provider-new-flow",
                        name="_too__run",
                        input={
                            "runnable": "flow:new_flow",
                            "input": {
                                "_": "topic",
                                "brief": {"title": "generated"},
                            },
                        },
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("finished")),
        ),
    )

    async def scenario() -> None:
        await watcher.refresh()
        flows = layout.home / "flows"
        flows.mkdir(parents=True, exist_ok=True)
        (flows / "new_flow.too").write_text(
            """struct Brief:
  title: Text

flow new_flow(_: Text, brief: Brief) -> Text:
  let result =
    {{brief.title}} {{_}}
""",
            encoding="utf-8",
        )
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == [
                "model",
                "tool",
                "model",
                "run",
                "model",
            ]
            assert steps[0].state == steps[1].state
            assert steps[0].state != steps[2].state
            assert steps[2].state == steps[3].state == steps[4].state
            controls = harness.store.list_run_controls(run_id=root.id)
            reload_control = next(item for item in controls if item.kind == "reload")
            assert reload_control.status == "applied"
            second_call = harness.adapter.invocations[1].call
            reload_result = second_call.messages[-1].parts[0]
            assert isinstance(reload_result, ToolResultPart)
            assert reload_result.error is None
            assert reload_result.output["applied"] is True
            assert "flow:new_flow" in second_call.instructions
            dynamic = steps[3]
            assert isinstance(dynamic.given, RunStmt)
            assert dynamic.given.runnable == "flow:new_flow"
            assert reload_control.triggered_by == steps[1].ref
            assert steps[2].preceded_by == (reload_control.ref,)
            history = harness.store.recent_conversation_messages(
                thread_id=thread,
            )
            assert any(
                isinstance(part, ToolResultPart) and part.tool_call_id == "call-reload"
                for message in history
                for part in message.parts
            )

    asyncio.run(scenario())


def test_reload_then_run_does_not_rebuild_the_calling_agic_frame(
    tmp_path: Path,
) -> None:
    source = """
agic parent(_: Text) -> Text:
  recall = none
  hands = flow:target
  context: none
  instruct: none
  user: {{_}}

flow target(_: Text) -> Text:
  let result =
    completed {{_}}
"""
    reloaded_source = """
flow target(_: Text) -> Text:
  let result =
    completed {{_}}
"""
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text(source, encoding="utf-8")
    initial = prepare_agent_state(layout)
    watcher = StateWatcher(layout)
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=initial,
        refresh_state=watcher.refresh_result,
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="reload-with-run",
                        call_id="provider-reload-with-run",
                        name="_too__reload",
                        input={},
                    ),
                    ToolCall(
                        tool_call_id="run-after-reload",
                        call_id="provider-run-after-reload",
                        name="_too__run",
                        input={
                            "runnable": "flow:target",
                            "input": {"_": "topic"},
                        },
                    ),
                )
            ),
        ),
    )

    async def scenario() -> None:
        await watcher.refresh()
        layout.program.write_text(reloaded_source, encoding="utf-8")
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "failed"
            assert "Runnable not found: parent" in str(root.error)
            steps = harness.store.list_steps(run_id=root.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "succeeded"),
                ("run", "succeeded"),
            ]
            assert steps[0].state != steps[2].state
            child = next(
                run
                for run in harness.store.list_run_tree(root_run_id=root.id)
                if run.parent == steps[2].ref
            )
            assert child.status == "succeeded"
            assert all(
                step.status != "running"
                for step in harness.store.list_steps(run_id=child.id)
            )
            assert len(harness.adapter.invocations) == 1

    asyncio.run(scenario())


def test_model_reload_applies_state_and_next_model_step_reports_missing_agic(
    tmp_path: Path,
) -> None:
    source = """
agic parent(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
"""
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text(source, encoding="utf-8")
    initial = prepare_agent_state(layout)
    watcher = StateWatcher(layout)
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=initial,
        refresh_state=watcher.refresh_result,
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="incompatible-reload",
                        call_id="provider-incompatible",
                        name="_too__reload",
                        input={},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("continued")),
        ),
    )

    async def scenario() -> None:
        await watcher.refresh()
        layout.program.write_text(
            source.replace("parent", "replacement"),
            encoding="utf-8",
        )
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "failed"
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == ["model", "tool"]
            reload_control = next(
                item
                for item in harness.store.list_run_controls(run_id=root.id)
                if item.kind == "reload"
            )
            assert reload_control.status == "applied"
            assert reload_control.error is None
            assert "Runnable not found: parent" in str(root.error)
            assert watcher.current().revision != initial.revision

    asyncio.run(scenario())


def test_dynamic_run_store_failure_fails_the_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text) -> Text:
  recall = none
  hands = flow:child
  context: none
  instruct: none
  user: {{_}}

flow child(_: Text) -> Text:
  let result =
    completed {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="run-with-store-failure",
                        call_id="provider-run-with-store-failure",
                        name="_too__run",
                        input={
                            "runnable": "flow:child",
                            "input": {"_": "topic"},
                        },
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("must not recover")),
        ),
    )
    accept_run = harness.store.accept_run

    def fail_child_acceptance(**kwargs: Any) -> Any:
        if kwargs["parent"] is not None:
            raise OSError("child persistence failed")
        return accept_run(**kwargs)

    monkeypatch.setattr(harness.store, "accept_run", fail_child_acceptance)

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "failed"
            steps = harness.store.list_steps(run_id=root.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("run", "failed"),
            ]
            assert root.error == ErrorRef(FieldRef.from_path(steps[1].ref, "error"))
            assert steps[1].error == ErrorMessage("child persistence failed")
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            assert len(harness.adapter.invocations) == 1

    asyncio.run(scenario())


def test_reload_returns_candidate_diagnostics_without_a_control(
    tmp_path: Path,
) -> None:
    source = """
agic parent(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
"""
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text(source, encoding="utf-8")
    initial = prepare_agent_state(layout)
    watcher = StateWatcher(layout)
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=initial,
        refresh_state=watcher.refresh_result,
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="invalid-reload",
                        call_id="provider-invalid",
                        name="_too__reload",
                        input={},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("continued")),
        ),
    )

    async def scenario() -> None:
        await watcher.refresh()
        flows = layout.home / "flows"
        flows.mkdir(parents=True, exist_ok=True)
        (flows / "research.too").write_text(
            "flow other:\n  pass\n",
            encoding="utf-8",
        )
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            assert all(
                item.kind != "reload"
                for item in harness.store.list_run_controls(run_id=root.id)
            )
            result = harness.adapter.invocations[1].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.error is None
            assert result.output["applied"] is False
            assert result.output["state"] == initial.revision
            diagnostics = result.output["diagnostics"]
            assert diagnostics[0]["code"] == "invalid-flow-export"

    asyncio.run(scenario())


def test_reload_of_unchanged_state_records_an_applied_noop_control(
    tmp_path: Path,
) -> None:
    source = """
agic parent(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
"""
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text(source, encoding="utf-8")
    initial = prepare_agent_state(layout)
    watcher = StateWatcher(layout)
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=initial,
        refresh_state=watcher.refresh_result,
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="unchanged-reload",
                        call_id="provider-unchanged",
                        name="_too__reload",
                        input={},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("continued")),
        ),
    )

    async def scenario() -> None:
        await watcher.refresh()
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == ["model", "tool", "model"]
            assert steps[0].state != steps[2].state
            assert harness.store.resolve_state_revision(steps[0].state) == (
                harness.store.resolve_state_revision(steps[2].state)
            )
            reload_control = next(
                item
                for item in harness.store.list_run_controls(run_id=root.id)
                if item.kind == "reload"
            )
            assert reload_control.status == "applied"
            result = harness.adapter.invocations[1].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.error is None
            assert result.output == {
                "applied": False,
                "from_state": initial.revision,
                "state": initial.revision,
                "control": {
                    "target": root.id,
                    "index": reload_control.index,
                },
                "diagnostics": [],
            }

    asyncio.run(scenario())


def test_reload_worker_failure_finishes_control_and_wakes_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = """
agic parent(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
"""
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text(source, encoding="utf-8")
    initial = prepare_agent_state(layout)
    watcher = StateWatcher(layout)
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=initial,
        refresh_state=watcher.refresh_result,
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="failed-reload",
                        call_id="provider-failed-reload",
                        name="_too__reload",
                        input={},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("recovered")),
        ),
    )
    finish = harness.store.finish_run_controls

    def fail_reload_control(**kwargs: Any) -> None:
        indexes = tuple(kwargs["indexes"])
        controls = tuple(
            harness.store.get_run_control(run_id=kwargs["run_id"], index=index)
            for index in indexes
        )
        if any(
            control is not None and control.kind == "reload" for control in controls
        ):
            raise RuntimeError("reload persistence failed")
        finish(**kwargs)

    monkeypatch.setattr(harness.store, "finish_run_controls", fail_reload_control)

    async def scenario() -> None:
        await watcher.refresh()
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            reload_control = next(
                item
                for item in harness.store.list_run_controls(run_id=root.id)
                if item.kind == "reload"
            )
            assert reload_control.status == "wontapply"
            assert reload_control.error == "reload persistence failed"
            result = harness.adapter.invocations[1].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.tool_call_id == "failed-reload"
            assert result.error == "reload persistence failed"

    asyncio.run(scenario())


def test_dynamic_run_uses_target_module_types_and_optional_arrays(
    tmp_path: Path,
) -> None:
    source = """
agic parent(_: Text) -> Text:
  recall = none
  hands = research
  context: none
  instruct: none
  user: {{_}}
"""
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text(source, encoding="utf-8")
    flows = layout.home / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    (flows / "research.too").write_text(
        """struct Brief:
  title: Text
  tags?: Text[]

agic echo(brief: Brief, prefix?: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{prefix}} {{brief.title}}

flow research(brief: Brief, prefix?: Text) -> Text:
  run echo
""",
        encoding="utf-8",
    )
    state = prepare_agent_state(layout)
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=state,
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="module-run",
                        call_id="provider-module-run",
                        name="_too__run",
                        input={
                            "runnable": "research",
                            "input": {
                                "brief": {
                                    "title": "Module-local input",
                                    "tags": ["runtime", "types"],
                                },
                                "prefix": "Review",
                            },
                        },
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("completed")),
            ModelCallResult(message=Message.assistant("parent finished")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:parent",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == ["model", "run", "model"]
            assert isinstance(steps[1].given, RunStmt)
            assert steps[1].given.runnable == "research"
            child = next(
                run
                for run in harness.store.list_run_tree(root_run_id=root.id)
                if run.parent == steps[1].ref
            )
            accepted = harness.store.get_run_control(run_id=child.id, index=0)
            assert accepted is not None
            assert isinstance(accepted.payload, RunControlPayload)
            assert accepted.payload.runnable == "_flow_research$flow:research"
            result = harness.adapter.invocations[2].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.error is None
            assert result.output == {
                "run_id": child.id,
                "runnable": "flow:research",
                "output_type": "Text",
                "output": "completed",
            }

    asyncio.run(scenario())


def test_execute_replaces_the_runnable_without_a_transition_step(
    tmp_path: Path,
) -> None:
    web = RecordingTool("web__search", output={"results": ["source"]})
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller(_: Text) -> Text:
  recall = none
  handoffs = agic:target
  context: none
  instruct: none
  user: Caller {{_}}

agic target(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: Target {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="handoff",
                        call_id="provider-handoff",
                        name="_too__execute",
                        input={"runnable": "target", "input": {"_": "work"}},
                    ),
                ),
                continuation={"id": "caller-cont"},
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="search",
                        call_id="provider-search",
                        name="web__search",
                        input={"query": "work"},
                    ),
                ),
            ),
            ModelCallResult(message=Message.assistant("completed")),
        ),
        tools={web.name: web},
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:caller",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            assert root.output is not None
            assert harness.store.resolve_value(root.output.value) == "completed"
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == [
                "model",
                "tool",
                "model",
                "tool",
                "model",
            ]
            controls = harness.store.list_run_controls(run_id=root.id, kind="execute")
            assert len(controls) == 1
            execute = controls[0]
            assert execute.status == "applied"
            assert isinstance(execute.payload, ExecuteControlPayload)
            source = FieldRef.from_path(steps[0].ref, "output", "value", 0)
            assert execute.payload.state == harness.state.revision
            assert execute.payload.runnable == "agent$agic:target"
            assert execute.triggered_by == steps[1].ref
            assert steps[2].preceded_by == (execute.ref,)
            assert len(execute.payload.input) == 1
            control_local = execute.payload.input[0]
            assert control_local.type == "Json"
            assert isinstance(control_local.value, TypedRef)
            assert control_local.value.ref == source.select("input", "input", "_")
            assert harness.store.resolve_local(control_local).value == "work"
            assert steps[2].input == (source.select("input", "input", "_"),)
            target_call = harness.adapter.invocations[1].call
            assert {
                tool.name for tool in harness.adapter.invocations[0].call.tools
            } == {
                "_too__execute",
                "_too__reload",
                "_too__run",
                "web__search",
            }
            assert {tool.name for tool in target_call.tools} == {
                "_too__execute",
                "_too__reload",
                "_too__run",
                "web__search",
            }
            assert {
                tool.name for tool in harness.adapter.invocations[2].call.tools
            } == {
                "_too__execute",
                "_too__reload",
                "_too__run",
                "web__search",
            }
            assert len(web.calls) == 1
            assert web.calls[0][0] == {"query": "work"}
            assert target_call.continuation is None
            assert [message.role for message in target_call.messages] == ["user"]
            assert "Target work" in str(target_call.messages[0].parts[0])

    asyncio.run(scenario())


def test_execute_resolves_a_fresh_structured_output_contract(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller(_: Text) -> Json:
  recall = none
  handoffs = agic:target
  context: none
  instruct: none
  user: Caller {{_}}

agic target(_: Text) -> Boolean:
  recall = none
  context: none
  instruct: none
  user: Target {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="handoff",
                        call_id="provider-handoff",
                        name="_too__execute",
                        input={"runnable": "target", "input": {"_": "work"}},
                    ),
                ),
            ),
            ModelCallResult(message=Message.assistant("true")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:caller",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            assert root.output is not None
            assert harness.store.resolve_value(root.output.value) is True
            assert harness.adapter.invocations[0].call.output_schema == {}
            assert harness.adapter.invocations[1].call.output_schema == {
                "type": "boolean"
            }

    asyncio.run(scenario())


def test_execute_failure_returns_to_the_calling_agic_without_a_control(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller(_: Text) -> Text:
  recall = none
  handoffs = agic:allowed
  context: none
  instruct: none
  user: {{_}}

agic allowed -> Text:
  Allowed.

agic blocked -> Text:
  Blocked.
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="blocked-handoff",
                        call_id="provider-blocked",
                        name="_too__execute",
                        input={"runnable": "agic:blocked"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("recovered")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:caller",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            steps = harness.store.list_steps(run_id=root.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "failed"),
                ("model", "succeeded"),
            ]
            assert not harness.store.list_run_controls(run_id=root.id, kind="execute")
            result = harness.adapter.invocations[1].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.tool_call_id == "blocked-handoff"
            assert result.error == (
                "runnable is not authorized by handoffs: agic:blocked"
            )

    asyncio.run(scenario())


def test_runtime_tools_are_available_without_routes_or_refresh(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller() -> Text:
  recall = none
  context: none
  instruct: none
  Call.
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "unavailable",
                        "provider-unavailable",
                        "_too__execute",
                        {"runnable": "target"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("recovered")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:caller")
            )

            assert root.status == "succeeded", root.error
            assert [step.kind for step in harness.store.list_steps(run_id=root.id)] == [
                "model",
                "tool",
                "model",
            ]
            first_call = harness.adapter.invocations[0].call
            assert {tool.name for tool in first_call.tools} == {
                "_too__execute",
                "_too__reload",
                "_too__run",
            }
            assert "<available-runnable-routes>" not in first_call.instructions
            assert '"runnables"' not in first_call.instructions
            assert "declares no hands or handoffs" in first_call.instructions
            result = harness.adapter.invocations[1].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.error == "Runnable not found: target"

    asyncio.run(scenario())


def test_reload_without_refresh_returns_a_correlated_runtime_error(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller() -> Text:
  recall = none
  context: none
  instruct: none
  Call.
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "reload",
                        "provider-reload",
                        "_too__reload",
                        {},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("recovered")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:caller")
            )

            assert root.status == "succeeded", root.error
            assert [step.kind for step in harness.store.list_steps(run_id=root.id)] == [
                "model",
                "tool",
                "model",
            ]
            assert not harness.store.list_run_controls(run_id=root.id, kind="reload")
            result = harness.adapter.invocations[1].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.tool_call_id == "reload"
            assert result.error == "Agent State refresh is unavailable in this executor"

    asyncio.run(scenario())


def test_execute_must_be_the_only_model_tool_call(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller() -> Text:
  recall = none
  handoffs = target
  context: none
  instruct: none
  Call.

agic target() -> Text:
  Target.
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "first",
                        "provider-first",
                        "_too__execute",
                        {"runnable": "target"},
                    ),
                    ToolCall(
                        "second",
                        "provider-second",
                        "_too__execute",
                        {"runnable": "target"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("recovered")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:caller")
            )

            assert root.status == "succeeded", root.error
            steps = harness.store.list_steps(run_id=root.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "failed"),
                ("tool", "failed"),
                ("model", "succeeded"),
            ]
            assert not harness.store.list_run_controls(run_id=root.id, kind="execute")
            results = tuple(
                part
                for message in harness.adapter.invocations[1].call.messages[-2:]
                for part in message.parts
            )
            assert len(results) == 2
            assert all(
                isinstance(item, ToolResultPart)
                and item.error
                == "_too/execute must be the only tool call in its Model Call"
                for item in results
            )

    asyncio.run(scenario())


def test_chained_execute_rejects_a_runnable_already_in_the_lineage(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller() -> Text:
  recall = none
  handoffs = target
  context: none
  instruct: none
  Call.

agic target() -> Text:
  recall = none
  handoffs = caller
  context: none
  instruct: none
  Target.
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "to-target",
                        "provider-target",
                        "_too__execute",
                        {"runnable": "target"},
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "to-caller",
                        "provider-caller",
                        "_too__execute",
                        {"runnable": "caller"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("target recovered")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:caller")
            )

            assert root.status == "succeeded", root.error
            steps = harness.store.list_steps(run_id=root.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "succeeded"),
                ("model", "succeeded"),
                ("tool", "failed"),
                ("model", "succeeded"),
            ]
            controls = harness.store.list_run_controls(run_id=root.id, kind="execute")
            assert len(controls) == 1
            assert isinstance(controls[0].payload, ExecuteControlPayload)
            assert controls[0].payload.runnable == "agent$agic:target"
            result = harness.adapter.invocations[2].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.error == (
                "_too/execute cannot call the current or an ancestor runnable: "
                "agic:caller"
            )

    asyncio.run(scenario())


def test_chained_execute_controls_keep_one_run_and_reach_the_final_target(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller() -> Text:
  recall = none
  handoffs = middle
  context: none
  instruct: none
  Call.

agic middle(_: Text) -> Text:
  recall = none
  handoffs = flow:deliver
  context: none
  instruct: none
  Middle {{_}}.

flow deliver(_: Text) -> Text:
  let result =
    delivered {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "to-middle",
                        "provider-middle",
                        "_too__execute",
                        {"runnable": "middle", "input": {"_": "work"}},
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "to-deliver",
                        "provider-deliver",
                        "_too__execute",
                        {"runnable": "flow:deliver", "input": {"_": "work"}},
                    ),
                )
            ),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:caller")
            )

            assert root.status == "succeeded", root.error
            assert len(harness.store.list_run_tree(root_run_id=root.id)) == 1
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == [
                "model",
                "tool",
                "model",
                "tool",
                "value",
            ]
            controls = harness.store.list_run_controls(run_id=root.id, kind="execute")
            assert [
                control.payload.runnable
                for control in controls
                if isinstance(control.payload, ExecuteControlPayload)
            ] == [
                "agent$agic:middle",
                "agent$flow:deliver",
            ]

    asyncio.run(scenario())


def test_run_call_rejects_an_active_ancestor_runnable(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow outer() -> Text:
  run inner

agic inner() -> Text:
  recall = none
  hands = flow:outer
  context: none
  instruct: none
  Inner.
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "run-ancestor",
                        "provider-ancestor",
                        "_too__run",
                        {"runnable": "flow:outer"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("recovered")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="flow:outer")
            )

            assert root.status == "succeeded", root.error
            tree = harness.store.list_run_tree(root_run_id=root.id)
            assert len(tree) == 2
            child = next(run for run in tree if run.id != root.id)
            steps = harness.store.list_steps(run_id=child.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("run", "failed"),
                ("model", "succeeded"),
            ]
            result = harness.adapter.invocations[1].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.error == (
                "_too/run cannot call the current or an ancestor runnable: flow:outer"
            )

    asyncio.run(scenario())


def test_execute_target_failure_does_not_restore_the_caller(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller() -> Text:
  handoffs = target
  Call.

agic target() -> Text:
  Fail.
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "handoff",
                        "provider-handoff",
                        "_too__execute",
                        {"runnable": "target"},
                    ),
                )
            ),
            RuntimeError("target provider failed"),
            ModelCallResult(message=Message.assistant("must not resume")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:caller")
            )

            assert root.status == "failed"
            assert root.error is not None
            assert "target provider failed" in harness.store.resolve_error(root.error)
            assert len(harness.adapter.invocations) == 2
            assert [step.kind for step in harness.store.list_steps(run_id=root.id)] == [
                "model",
                "tool",
                "model",
            ]
            controls = harness.store.list_run_controls(run_id=root.id, kind="execute")
            assert len(controls) == 1 and controls[0].status == "applied"

    asyncio.run(scenario())


def test_execute_preserves_the_entry_output_contract(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller() -> Number:
  handoffs = target
  Call.

agic target() -> Text:
  Target.
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "handoff",
                        "provider-handoff",
                        "_too__execute",
                        {"runnable": "target"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("not a number")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:caller")
            )

            assert root.status == "failed"
            assert "Number" in str(root.error)
            steps = harness.store.list_steps(run_id=root.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "succeeded"),
                ("model", "succeeded"),
            ]

    asyncio.run(scenario())


def test_dynamic_public_agic_keeps_its_resource_scope_after_reload(
    tmp_path: Path,
) -> None:
    source = """
flow outer(_: Text) -> Text:
  run caller

agic caller(_: Text) -> Text:
  recall = none
  hands = agic:target
  context: none
  instruct: none
  user: {{_}}

agic target(_: Text) -> Text:
  recall = none
  tools = beta/*
  context: none
  instruct:
    old target state
  user: {{_}}
"""
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text(source, encoding="utf-8")
    initial = prepare_agent_state(layout)
    watcher = StateWatcher(layout)
    tools = {"beta__use": RecordingTool("beta__use", output={})}
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=initial,
        refresh_state=watcher.refresh_result,
        tools=tools,
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="run-public-target",
                        call_id="provider-run-public-target",
                        name="_too__run",
                        input={
                            "runnable": "agic:target",
                            "input": {"_": "topic"},
                        },
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="reload-public-target",
                        call_id="provider-reload-public-target",
                        name="_too__reload",
                        input={},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("target completed")),
            ModelCallResult(message=Message.assistant("caller completed")),
        ),
    )

    async def scenario() -> None:
        await watcher.refresh()
        layout.program.write_text(
            source.replace("old target state", "new target state"),
            encoding="utf-8",
        )
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:outer",
                    primary=resolve_input_parts("start"),
                )
            )

            assert root.status == "succeeded", root.error
            before_reload = harness.adapter.invocations[1].call
            after_reload = harness.adapter.invocations[2].call
            assert {tool.name for tool in before_reload.tools} == {
                "_too__execute",
                "_too__reload",
                "_too__run",
                "beta__use",
            }
            assert {tool.name for tool in after_reload.tools} == {
                "_too__execute",
                "_too__reload",
                "_too__run",
                "beta__use",
            }
            assert "old target state" in before_reload.instructions
            assert "new target state" in after_reload.instructions

    asyncio.run(scenario())


def harness_layout(root: Path) -> AgentLayout:
    return AgentLayout.resident(root, "alice")
