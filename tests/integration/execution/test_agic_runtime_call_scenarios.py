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
)
from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.common.layout import AgentLayout
from toolang.execution.executor.steps.tool import invoke_tool_call
from toolang.execution.records import RunControlPayload
from toolang.execution.tools.runtime import create_toolset
from toolang.execution.types import Pointer, ThreadPrefix, TypedPointer
from toolang.lang.ast import RunStmt
from toolang.lang.input import resolve_input_parts
from toolang.plugin.toolsets.loading import LoadedTool
from toolang.plugin.toolsets.registry import ToolRef
from toolang.state.prepare import prepare_agent_state
from toolang.state.watcher import StateWatcher


def _runtime_tools() -> dict[str, LoadedTool]:
    toolset = create_toolset({})
    return {
        ref.model_name: LoadedTool(
            plugin_name="_too",
            ref=ref,
            leaf_tool=leaf,
        )
        for name, leaf in toolset.tools().items()
        for ref in (ToolRef(plugin="_too", toolset="_too", name=name),)
    }


def test_agic_dynamic_run_is_one_run_step_and_one_child(tmp_path: Path) -> None:
    tools = _runtime_tools()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text) -> Text:
  recall = none
  tools = _too/run
  context: none
  instruct: none
  user: {{_}}

agic child(_: Text) -> Text:
  recall = none
  tools = -_too/*
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
        tools=tools,
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
            assert dynamic.input == (Pointer.step(root_steps[0].path, 0),)
            dynamic_output = dynamic.output
            assert dynamic_output is not None
            assert isinstance(dynamic_output.value, TypedPointer)
            assert harness.store.resolve_value(dynamic_output.value) == "child output"
            children = [
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.parent is not None
            ]
            assert len(children) == 1
            assert children[0].parent == dynamic.path
            child_control = harness.store.get_run_control(
                run_id=children[0].control.target,
                index=children[0].control.index,
            )
            assert child_control is not None
            assert isinstance(child_control.payload, RunControlPayload)
            assert child_control.payload.runnable == "agic:child"
            assert all(step.kind != "tool" for step in root_steps)
            followup = harness.adapter.invocations[2].call
            result = followup.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.tool_call_id == "call-run"
            assert result.output["run_id"] == children[0].id
            assert "<available-runnables>" in (
                harness.adapter.invocations[0].call.instructions
            )
            assert "<available-runnables>" not in (
                harness.adapter.invocations[1].call.instructions
            )
            assert harness.adapter.invocations[1].call.tools == ()
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


def test_runtime_action_rejects_generic_tool_invocation(tmp_path: Path) -> None:
    tools = _runtime_tools()
    reload_tool = tools["_too__reload"]

    result = asyncio.run(
        invoke_tool_call(
            run_id="run-test",
            tools=tools,
            services=(),
            layout=harness_layout(tmp_path),
            call=ToolCall(
                tool_call_id="reload",
                call_id="provider-reload",
                name=reload_tool.name,
                input={},
            ),
        )
    )

    assert result.output == {}
    assert result.error == (
        "runtime action must be handled by the agic executor: reload"
    )


def test_invalid_dynamic_run_records_failure_and_model_recovers(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text) -> Text:
  recall = none
  tools = _too/run
  context: none
  instruct: none
  user: {{_}}
""",
        tools=_runtime_tools(),
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
  tools = _too/run
  context: none
  instruct: none
  user: {{_}}

agic child(_: Text) -> Text:
  recall = none
  tools = -_too/*
  context: none
  instruct: none
  user: {{_}}
""",
        tools=_runtime_tools(),
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
                if run.parent == dynamic.path
            )
            assert child.status == "failed"
            assert dynamic.error == Pointer.run(child.id)
            result = harness.adapter.invocations[2].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.tool_call_id == "failed-child"
            assert "child provider failed" in (result.error or "")

    asyncio.run(scenario())


def test_reload_has_no_step_and_new_flow_runs_in_same_root(tmp_path: Path) -> None:
    source = """
agic parent(_: Text) -> Text:
  recall = none
  tools = _too/*
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
        tools=_runtime_tools(),
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
  let result:
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
                "model",
                "run",
                "model",
            ]
            assert steps[0].state != steps[1].state
            assert steps[1].state == steps[2].state == steps[3].state
            controls = harness.store.list_run_controls(run_id=root.id)
            reload_control = next(item for item in controls if item.kind == "reload")
            assert reload_control.status == "applied"
            second_call = harness.adapter.invocations[1].call
            reload_result = second_call.messages[-1].parts[0]
            assert isinstance(reload_result, ToolResultPart)
            assert reload_result.error is None
            assert reload_result.output["applied"] is True
            assert "flow:new_flow" in second_call.instructions
            dynamic = steps[2]
            assert isinstance(dynamic.given, RunStmt)
            assert dynamic.given.runnable == "flow:new_flow"
            assert all(step.kind != "tool" for step in steps)
            history = harness.store.recent_conversation_messages(
                thread_id=thread,
            )
            assert all(
                not isinstance(part, ToolResultPart)
                or part.tool_call_id != "call-reload"
                for message in history
                for part in message.parts
            )

    asyncio.run(scenario())


def test_model_reload_applies_state_and_next_model_step_reports_missing_agic(
    tmp_path: Path,
) -> None:
    source = """
agic parent(_: Text) -> Text:
  recall = none
  tools = _too/reload
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
        tools=_runtime_tools(),
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
            assert [step.kind for step in steps] == ["model"]
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


def test_reload_returns_candidate_diagnostics_without_a_control(
    tmp_path: Path,
) -> None:
    source = """
agic parent(_: Text) -> Text:
  recall = none
  tools = _too/reload
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
        tools=_runtime_tools(),
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
  tools = _too/reload
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
        tools=_runtime_tools(),
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
            assert [step.kind for step in steps] == ["model", "model"]
            assert steps[0].state != steps[1].state
            assert harness.store.resolve_state_revision(steps[0].state) == (
                harness.store.resolve_state_revision(steps[1].state)
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
  tools = _too/reload
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
        tools=_runtime_tools(),
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
  tools = _too/run
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
  tools = -_too/*
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
        tools=_runtime_tools(),
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
                if run.parent == steps[1].path
            )
            accepted = harness.store.get_run_control(run_id=child.id, index=0)
            assert accepted is not None
            assert isinstance(accepted.payload, RunControlPayload)
            assert accepted.payload.runnable == "flow:research"
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


def harness_layout(root: Path) -> AgentLayout:
    return AgentLayout.resident(root, "alice")
