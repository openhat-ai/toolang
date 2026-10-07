"""Agic-owned published State and public runnable call scenarios."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    assert_run_event_integrity,
    last_tool_result,
    route_snapshots,
    without_runtime_snapshots,
)
from tests.support.execution_harness import (
    ExecutionHarness,
    RecordingRunTracer,
    PublicationTracer,
    RecordingTool,
)
from toolang.base.types.message import ImagePart, Message, TextPart, ToolResultPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.base.types.tool import ToolContext
from toolang.common.layout import AgentLayout
from toolang.execution.executor.steps.tool import invoke_tool_call
from toolang.execution.store import RunStore
from toolang.execution.values import parts_from_value
from toolang.execution.records import (
    ExecControlPayload,
    RunControlPayload,
)
from toolang.execution.types import (
    ErrorMessage,
    ErrorRef,
    FieldRef,
    ThreadPrefix,
    ToolStepGiven,
)
from toolang.lang.input import resolve_input_parts
from toolang.lang.types import Array, Struct
from toolang.state.prepare import prepare_agent_state
from toolang.state.watcher import StateWatcher


@pytest.mark.parametrize("action", ["exec", "run", "async run", "spawn"])
@pytest.mark.parametrize(
    "type_name,raw,expected",
    [
        ("Part[]", "work", Array("Part[]", (TextPart("work"),))),
        ("Part[]", [], Array("Part[]", ())),
        (
            "Part[]",
            [
                {"type": "text", "text": "work"},
                {"type": "image", "image_url": "https://example.com/a.png"},
            ],
            Array("Part[]", (TextPart("work"), ImagePart("https://example.com/a.png"))),
        ),
        ("Part", "work", TextPart("work")),
        ("Number", "7", 7),
        ("Boolean", "true", True),
        ("Json", "false", "false"),
        ("Number[]", "[1,2]", Array("Number[]", (1, 2))),
        (
            "Number[][]",
            [[1, 2], []],
            Array("Number[][]", (Array("Number[]", (1, 2)), Array("Number[]", ()))),
        ),
        (
            "Packet",
            {"content": ["work"]},
            Struct("Packet", {"content": Array("Part[]", (TextPart("work"),))}),
        ),
    ],
)
def test_runtime_calls_persist_bound_inputs(
    tmp_path, monkeypatch, action, type_name, raw, expected
):
    arguments: dict[str, Any] = {
        "runnable": "flow:target",
        "input": {"_": raw, "note": "extra"},
    }
    if action == "async run":
        arguments["async"] = True
    call = ToolCall(
        "start",
        "provider-start",
        f"_toolang__{'run' if action == 'async run' else action}",
        arguments,
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
struct Packet:
  content: Part[]
agic caller() -> {type_name if action == "exec" else "Text"}:
  recall = none
  context = none
  instruct = none
  user: Call target.
flow target(_: {type_name}, note: Part[]) -> {type_name}:
  pass
""",
        responses=[],
    )
    calls = []

    async def invoke(model, request, *, environ, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            return ModelCallResult(tool_calls=(call,))
        result = last_tool_result(request)
        assert result.error is None, result
        if len(calls) == 2 and action in ("async run", "spawn"):
            return ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "wait",
                        "provider-wait",
                        "_toolang__await",
                        {"target": result.output["id"]},
                    ),
                )
            )
        return ModelCallResult(message=Message.assistant("done"))

    monkeypatch.setattr(harness.adapter, "invoke", invoke)
    monkeypatch.setattr(harness.adapter, "stream", invoke)
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="caller",
                ),
                tracer=tracer,
            )
            assert root.status == "succeeded", root.error
            if harness.executor._tasks:
                await asyncio.gather(*harness.executor._tasks)
            controls = [
                control
                for run in harness.store.list_runs(limit=None)
                for control in harness.store.list_run_controls(run_id=run.id)
                if isinstance(control.payload, (RunControlPayload, ExecControlPayload))
                and control.payload.runnable == "flow:target"
            ]
            assert len(controls) == 1
            control = controls[0]
            assert isinstance(control.payload, (RunControlPayload, ExecControlPayload))
            assert control.payload.input["_"] == expected
            assert control.payload.input["note"] == Array(
                "Part[]", (TextPart("extra"),)
            )
            target = harness.store.get_run(run_id=control.ref.target.id)
            assert target is not None and target.output is not None
            assert harness.store.resolve_value(target.output.value) == expected
            step = harness.store.list_steps(run_id=root.id)[1]
            assert isinstance(step.given, ToolStepGiven)
            assert step.given.call == call
            return target.id

    target_id = asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
    reopened = RunStore(harness.store.db_path, read_only=True)
    try:
        target = reopened.get_run(run_id=target_id)
        assert target is not None and target.output is not None
        assert reopened.resolve_value(target.output.value) == expected
    finally:
        reopened.close()


@pytest.mark.parametrize("action", ["exec", "run", "async run", "spawn"])
@pytest.mark.parametrize(
    "input",
    [
        {"_": "bad", "count": "8"},
        {"_": "7", "count": "bad"},
        {"_": True, "count": 8},
        {"_": "7"},
        {"_": "7", "count": 8, "extra": 1},
    ],
)
def test_runtime_calls_reject_invalid_input_before_admission(tmp_path, action, input):
    arguments = {
        "runnable": "target",
        "input": input,
        **({"async": True} if action == "async run" else {}),
    }
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller() -> Text:
  recall = none
  user: Call target.
flow target(_: Number, count: Number) -> Number:
  pass
""",
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "bad",
                        "bad",
                        f"_toolang__{'run' if action == 'async run' else action}",
                        arguments,
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="caller",
                )
            )
            assert root.status == "succeeded", root.error
            result = last_tool_result(harness.adapter.invocations[1].call)
            assert result.error is not None
            assert harness.store.list_runs(limit=None) == [root]
            assert not harness.store.list_run_controls(run_id=root.id, kind="exec")

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("directive", ["hands", "handoffs"])
def test_named_main_can_be_called_through_authorized_routes(
    tmp_path: Path, kind: str, directive: str
) -> None:
    target = (
        "agic main() -> Text:\n  recall = none\n  context = none\n  user: Main.\n"
        if kind == "agic"
        else "flow main() -> Text:\n  run helper\n"
    )
    source = f"""
agic caller() -> Text:
  recall = none
  {directive} = {kind}:main
  user: Caller.

agic helper() -> Text:
  recall = none
  context = none
  user: Main.

## Use this entry for the general request.
{target}
"""
    action = "run" if directive == "hands" else "exec"
    responses = [
        ModelCallResult(
            tool_calls=(
                ToolCall(
                    "main-call",
                    "main-call",
                    f"_toolang__{action}",
                    {"runnable": f"{kind}:main"},
                ),
            )
        ),
        ModelCallResult(message=Message.assistant("main output")),
    ]
    if directive == "hands":
        responses.append(ModelCallResult(message=Message.assistant("caller output")))
    harness = ExecutionHarness.create(tmp_path, source=source, responses=responses)

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:caller")
            )

            assert root.status == "succeeded", root.error
            targets = route_snapshots(harness.adapter.invocations[0].call)[directive]
            assert len(targets) == 1
            assert targets[0]["ref"] == f"{kind}:main"
            assert (
                targets[0]["documentation"] == "Use this entry for the general request."
            )
            child_call = harness.adapter.invocations[1].call
            child_routes = route_snapshots(child_call)
            if directive == "hands":
                assert child_routes[directive] == []
                assert [item["ref"] for item in child_routes["handoffs"]] == (
                    ["agic:helper"] if kind == "agic" else []
                )
            else:
                # Exec removes the caller's rules and identity from the path.
                expected = (
                    ["agic:caller", "agic:helper"]
                    if kind == "agic"
                    else ["agic:caller"]
                )
                assert [item["ref"] for item in child_routes["hands"]] == expected
                assert [item["ref"] for item in child_routes["handoffs"]] == (
                    expected + ["agic:main"] if kind == "agic" else expected
                )
            assert without_runtime_snapshots(child_call.messages) == [
                Message.user("Main.")
            ]
            controls = [
                control
                for run in harness.store.list_run_tree(root_run_id=root.id)
                for control in harness.store.list_run_controls(run_id=run.id)
            ]
            assert any(
                isinstance(control.payload, (RunControlPayload, ExecControlPayload))
                and control.payload.runnable == f"{kind}:main"
                for control in controls
            )
            assert root.output is not None
            assert harness.store.resolve_value(root.output.value) == (
                "caller output" if directive == "hands" else "main output"
            )

    asyncio.run(scenario())


def test_agic_dynamic_run_is_one_tool_step_and_one_child(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text) -> Text:
  recall = none
  hands = agic:child
  context = none
  instruct = none
  user: {{_}}

agic child(_: Text) -> Text:
  recall = none
  context = none
  instruct = none
  user: Child {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call-run",
                        call_id="provider-run",
                        name="_toolang__run",
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
            assert [step.kind for step in root_steps] == ["model", "tool", "model"]
            dynamic = root_steps[1]
            assert isinstance(dynamic.given, ToolStepGiven)
            assert dynamic.given.call.input["runnable"] == "agic:child"
            assert dynamic.input == (
                FieldRef.from_path(root_steps[0].ref, "output", "value", 0),
            )
            dynamic_output = dynamic.output
            assert dynamic_output is not None
            (persisted_result,) = parts_from_value(dynamic_output.value)
            assert isinstance(persisted_result, ToolResultPart)
            assert persisted_result.output == {"type": "Text", "value": "child output"}
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
            assert child_control.payload.runnable == "agic:child"
            followup = harness.adapter.invocations[2].call
            result = last_tool_result(followup)
            assert isinstance(result, ToolResultPart)
            assert result.tool_call_id == "call-run"
            assert persisted_result == result
            assert child_control.status == "applied"
            assert not any(m.tag == "run-result" for m in followup.messages)
            parent_routes = route_snapshots(harness.adapter.invocations[0].call)
            assert [item["ref"] for item in parent_routes["hands"]] == ["agic:child"]
            assert [item["ref"] for item in parent_routes["handoffs"]] == [
                "agic:parent",
                "agic:child",
            ]
            assert route_snapshots(harness.adapter.invocations[1].call) == {
                "hands": [],
                "handoffs": [],
            }
            prohibitions = harness.adapter.invocations[1].call.instructions.split(
                "## Don't", 1
            )[1]
            assert "call run, spawn, or exec without authorized routes" in prohibitions
            assert {
                tool.name for tool in harness.adapter.invocations[1].call.tools
            } == {
                "_toolang__chdir",
                "_toolang__exec",
                "_toolang__pick",
                "_toolang__run",
                "_toolang__spawn",
                "_toolang__await",
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
  context = none
  instruct = none
  user: {{_}}

agic reviewer(_: Part[]) -> Text:
  recall = none
  context = none
  instruct = none
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
                        name="_toolang__run",
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
            assert reviewer_call.messages[-3] == Message.user(
                '<toolang:hands enabled="false" requested_only="false"/>\n<toolang:handoffs enabled="false" requested_only="true"/>\n\nReview candidate'
            )

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
  context = none
  instruct = none
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
                        name="_toolang__run",
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
            result = last_tool_result(harness.adapter.invocations[1].call)
            assert isinstance(result, ToolResultPart)
            assert result.error == "missing named inputs for check: threshold"
            assert result.output == {
                "code": "invalid_runnable_input",
                "runnable": "flow:check",
                "expected": {
                    "input": {"optional": False, "type": "Text", "documentation": ""},
                    "parameters": [
                        {
                            "name": "threshold",
                            "optional": False,
                            "type": "Number",
                            "documentation": "",
                        }
                    ],
                    "structs": [],
                    "output": "Text",
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
  context = none
  instruct = none
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
                        name="_toolang__run",
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
                        name="_toolang__run",
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
                ("tool", "failed"),
                ("model", "succeeded"),
                ("tool", "succeeded"),
                ("model", "succeeded"),
            ]

    asyncio.run(scenario())


def test_wildcard_routes_hide_active_lineage_and_keep_completed_children(
    tmp_path: Path,
) -> None:
    responses = []
    for index in range(2):
        responses.extend(
            (
                ModelCallResult(
                    tool_calls=(
                        ToolCall(
                            f"run-{index}",
                            f"provider-{index}",
                            "_toolang__run",
                            {"runnable": "helper"},
                        ),
                    )
                ),
                ModelCallResult(message=Message.assistant("helper completed")),
            )
        )
    responses.append(ModelCallResult(message=Message.assistant("done")))
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic default() -> Text:
  hands = *
  handoffs = *
  context = none
  Delegate the task.

agic helper() -> Text:
  hands = *
  handoffs = *
  context = none
  Help.
""",
        responses=responses,
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:default"),
                tracer=tracer,
            )
            assert root.status == "succeeded", root.error
            assert len(harness.store.list_run_tree(root_run_id=root.id)) == 3
            calls = [invocation.call for invocation in harness.adapter.invocations]
            assert len(calls) == 5
            for index, call in enumerate(calls):
                snapshots = route_snapshots(call)
                assert [target["ref"] for target in snapshots["hands"]] == (
                    ["agic:helper"] if index % 2 == 0 else []
                )
                assert [target["ref"] for target in snapshots["handoffs"]] == (
                    ["agic:default", "agic:helper"] if index % 2 == 0 else []
                )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("count", [63, 64])
@pytest.mark.parametrize("directives", ["", "  hands = *\n  handoffs = *\n"])
def test_route_limit_includes_the_root_self_handoff(
    tmp_path: Path, count: int, directives: str
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=(
            f"agic default() -> Text:\n{directives}  Call.\n"
            + "\n".join(
                f"agic action_{index}() -> Text:\n  Help.\n" for index in range(count)
            )
        ),
        responses=(ModelCallResult(message=Message.assistant("done")),),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:default")
            )
            if count == 63:
                assert root.status == "succeeded", root.error
                snapshots = route_snapshots(harness.adapter.invocations[0].call)
                assert len(snapshots["hands"]) == count
                assert len(snapshots["handoffs"]) == count + 1
                assert all(
                    target["ref"] != "agic:default" for target in snapshots["hands"]
                )
                assert snapshots["handoffs"][0]["ref"] == "agic:default"
            else:
                assert root.status == "failed"
                assert root.error == ErrorMessage(
                    "Authorized routes exceed 64 targets. Narrow hands or handoffs."
                )
                assert harness.adapter.invocations == []

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
  context = none
  instruct = none
  user: {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="run-self",
                        call_id="provider-run-self",
                        name="_toolang__run",
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
                ("tool", "failed"),
                ("model", "succeeded"),
            ]
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            result = last_tool_result(harness.adapter.invocations[1].call)
            assert isinstance(result, ToolResultPart)
            assert result.error == (
                "_toolang/run cannot call the current or an ancestor runnable: agic:parent"
            )
            assert result.output == {}
            for invocation in harness.adapter.invocations:
                assert route_snapshots(invocation.call)["hands"] == []
                assert [
                    item["ref"] for item in route_snapshots(invocation.call)["handoffs"]
                ] == ["agic:parent"]

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
  context = none
  instruct = none
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
                        name="_toolang__run",
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
                ("tool", "failed"),
                ("model", "succeeded"),
            ]
            result = next(
                part
                for message in harness.adapter.invocations[1].call.messages
                for part in message.parts
                if isinstance(part, ToolResultPart)
                and part.tool_name == "_toolang__run"
            )
            assert result.error == (
                "_toolang/run cannot call the current or an ancestor runnable: flow:outer"
            )
            assert result.output == {}
            for invocation in harness.adapter.invocations:
                assert route_snapshots(invocation.call) == {"hands": [], "handoffs": []}

    asyncio.run(scenario())


def test_dynamic_run_rejects_published_ancestor_by_public_identity(
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
  context = none
  instruct = none
  user: {{_}}

flow -> Text:
  run caller
"""
    published_flow = initial_flow.replace(
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
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="publication-ancestor",
                        call_id="provider-publication-ancestor",
                        name="test__checkpoint",
                        input={},
                    ),
                    ToolCall(
                        tool_call_id="run-published-ancestor",
                        call_id="provider-run-published-ancestor",
                        name="_toolang__run",
                        input={
                            "runnable": "flow:outer",
                            "input": {"_": "again"},
                        },
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("recovered")),
        ),
        tools={"test__checkpoint": RecordingTool("test__checkpoint", output={})},
    )

    async def scenario() -> None:
        await watcher.refresh()
        flow_source.write_text(published_flow, encoding="utf-8")
        tracer = PublicationTracer(
            harness, {"publication-ancestor": await watcher.refresh()}
        )
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:outer",
                    primary=resolve_input_parts("start"),
                ),
                tracer=tracer,
            )

            assert root.status == "succeeded", root.error
            runs = harness.store.list_run_tree(root_run_id=root.id)
            assert len(runs) == 2
            caller = next(run for run in runs if run.id != root.id)
            steps = harness.store.list_steps(run_id=caller.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "succeeded"),
                ("tool", "failed"),
                ("model", "succeeded"),
            ]
            result = next(
                part
                for message in harness.adapter.invocations[1].call.messages
                for part in message.parts
                if isinstance(part, ToolResultPart)
                and part.tool_name == "_toolang__run"
            )
            assert isinstance(result, ToolResultPart)
            assert result.error == (
                "_toolang/run cannot call the current or an ancestor runnable: flow:outer"
            )
            for invocation in harness.adapter.invocations:
                assert route_snapshots(invocation.call) == {"hands": [], "handoffs": []}

    asyncio.run(scenario())


def test_generic_dispatch_requires_per_call_runtime_authority(tmp_path: Path) -> None:
    from toolang.plugin.toolsets.loading import load_tools

    call = ToolCall("execute", "provider-execute", "_toolang__exec", {})
    context = ToolContext(tmp_path, tmp_path)
    result = asyncio.run(
        invoke_tool_call(
            call=call,
            tool=load_tools(queries=("_toolang/*",))[call.name],
            context=context,
        )
    )

    assert result.output == {}
    assert result.error == ("runtime operations are unavailable for this tool call")


def test_invalid_dynamic_run_records_failure_and_model_recovers(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text) -> Text:
  recall = none
  hands = missing
  context = none
  instruct = none
  user: {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="bad-run",
                        call_id="provider-bad-run",
                        name="_toolang__run",
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
            assert [step.kind for step in steps] == ["model", "tool", "model"]
            assert steps[1].status == "failed"
            assert isinstance(steps[1].given, ToolStepGiven)
            assert steps[1].given.call.input == {"input": {}}
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            result = last_tool_result(harness.adapter.invocations[1].call)
            assert isinstance(result, ToolResultPart)
            assert result.tool_call_id == "bad-run"
            assert result.error == "_toolang/run requires a non-empty runnable ref"

    asyncio.run(scenario())


def test_dynamic_child_failure_reports_error_and_model_recovers(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent(_: Text) -> Text:
  recall = none
  hands = agic:child
  context = none
  instruct = none
  user: {{_}}

agic child(_: Text) -> Text:
  recall = none
  context = none
  instruct = none
  user: {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="failed-child",
                        call_id="provider-failed-child",
                        name="_toolang__run",
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
            assert [step.kind for step in steps] == ["model", "tool", "model"]
            dynamic = steps[1]
            assert dynamic.status == "failed"
            child = next(
                run
                for run in harness.store.list_run_tree(root_run_id=root.id)
                if run.parent == dynamic.ref
            )
            assert child.status == "failed"
            assert isinstance(dynamic.error, ErrorRef)
            assert child.error is not None
            assert "child provider failed" in harness.store.resolve_error(child.error)
            result = last_tool_result(harness.adapter.invocations[2].call)
            assert isinstance(result, ToolResultPart)
            assert result.tool_call_id == "failed-child"
            assert result.error is not None and "child provider failed" in result.error
            assert not any(
                m.tag == "run-result"
                for m in harness.adapter.invocations[2].call.messages
            )

    asyncio.run(scenario())


def test_publication_then_run_does_not_rebuild_the_calling_agic_frame(
    tmp_path: Path,
) -> None:
    source = """
agic parent(_: Text) -> Text:
  recall = none
  hands = flow:target
  context = none
  instruct = none
  user: {{_}}

flow target(_: Text) -> Text:
  let result =
    completed {{_}}
"""
    published_source = """
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
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="publication-with-run",
                        call_id="provider-publication-with-run",
                        name="test__checkpoint",
                        input={},
                    ),
                    ToolCall(
                        tool_call_id="run-after-publication",
                        call_id="provider-run-after-publication",
                        name="_toolang__run",
                        input={
                            "runnable": "flow:target",
                            "input": {"_": "topic"},
                        },
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("continued")),
        ),
        tools={"test__checkpoint": RecordingTool("test__checkpoint", output={})},
    )

    async def scenario() -> None:
        await watcher.refresh()
        layout.program.write_text(published_source, encoding="utf-8")
        harness.published = await watcher.refresh()
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
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "succeeded"),
                ("tool", "succeeded"),
                ("model", "succeeded"),
            ]
            assert steps[0].state == steps[2].state
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
            assert len(harness.adapter.invocations) == 2

    asyncio.run(scenario())


def test_model_publication_keeps_an_active_agic_deleted_from_latest_state(
    tmp_path: Path,
) -> None:
    source = """
agic parent(_: Text) -> Text:
  recall = none
  context = none
  instruct = none
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
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="incompatible-publication",
                        call_id="provider-incompatible",
                        name="_toolang__chdir",
                        input={"path": "lab://"},
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
        harness.published = await watcher.refresh()
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
  context = none
  instruct = none
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
                        name="_toolang__run",
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
                ("tool", "failed"),
            ]
            assert root.error == ErrorRef(FieldRef.from_path(steps[1].ref, "error"))
            assert steps[1].error == ErrorMessage("child persistence failed")
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            assert len(harness.adapter.invocations) == 1

    asyncio.run(scenario())


def test_dynamic_run_uses_target_module_types_and_optional_arrays(
    tmp_path: Path,
) -> None:
    source = """
agic parent(_: Text) -> Text:
  recall = none
  hands = research
  context = none
  instruct = none
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
  context = none
  instruct = none
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
                        name="_toolang__run",
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
            assert [step.kind for step in steps] == ["model", "tool", "model"]
            assert isinstance(steps[1].given, ToolStepGiven)
            assert steps[1].given.call.input["runnable"] == "research"
            child = next(
                run
                for run in harness.store.list_run_tree(root_run_id=root.id)
                if run.parent == steps[1].ref
            )
            accepted = harness.store.get_run_control(run_id=child.id, index=0)
            assert accepted is not None
            assert isinstance(accepted.payload, RunControlPayload)
            assert accepted.payload.runnable == "flows::research::flow:research"
            result = last_tool_result(harness.adapter.invocations[2].call)
            assert isinstance(result, ToolResultPart)
            assert result.error is None
            assert result.output == {"type": "Text", "value": "completed"}

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
  context = none
  instruct = none
  user: Caller {{_}}

agic target(_: Text) -> Text:
  recall = none
  context = none
  instruct = none
  user: Target {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="handoff",
                        call_id="provider-handoff",
                        name="_toolang__exec",
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
            controls = harness.store.list_run_controls(run_id=root.id, kind="exec")
            assert len(controls) == 1
            execute = controls[0]
            assert execute.status == "applied"
            assert isinstance(execute.payload, ExecControlPayload)
            assert execute.payload.state == harness.state.revision
            assert execute.payload.runnable == "agic:target"
            assert execute.triggered_by == steps[1].ref
            assert steps[2].preceded_by == (execute.ref,)
            assert len(execute.payload.input) == 1
            control_value = execute.payload.input["_"]
            assert control_value == "work"
            assert steps[2].input == ()
            target_call = harness.adapter.invocations[1].call
            assert {
                tool.name for tool in harness.adapter.invocations[0].call.tools
            } == {
                "_toolang__chdir",
                "_toolang__exec",
                "_toolang__pick",
                "_toolang__run",
                "_toolang__spawn",
                "_toolang__await",
                "web__search",
            }
            assert {tool.name for tool in target_call.tools} == {
                "_toolang__chdir",
                "_toolang__exec",
                "_toolang__pick",
                "_toolang__run",
                "_toolang__spawn",
                "_toolang__await",
                "web__search",
            }
            assert {
                tool.name for tool in harness.adapter.invocations[2].call.tools
            } == {
                "_toolang__chdir",
                "_toolang__exec",
                "_toolang__pick",
                "_toolang__run",
                "_toolang__spawn",
                "_toolang__await",
                "web__search",
            }
            assert len(web.calls) == 1
            assert web.calls[0][0] == {"query": "work"}
            assert target_call.continuation is None
            assert [
                message.role
                for message in without_runtime_snapshots(target_call.messages)
            ] == ["user"]
            assert "Target work" in str(target_call.messages[0].parts[0])

    asyncio.run(scenario())


def test_execute_resolves_a_fresh_structured_output_contract(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller(_: Text) -> Json:
  recall = none
  handoffs = agic:target
  context = none
  instruct = none
  user: Caller {{_}}

agic target(_: Text) -> Boolean:
  recall = none
  context = none
  instruct = none
  user: Target {{_}}
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="handoff",
                        call_id="provider-handoff",
                        name="_toolang__exec",
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
  context = none
  instruct = none
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
                        name="_toolang__exec",
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
            assert not harness.store.list_run_controls(run_id=root.id, kind="exec")
            result = last_tool_result(harness.adapter.invocations[1].call)
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
  hands = none
  handoffs = none
  recall = none
  context = none
  instruct = none
  Call.
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "unavailable",
                        "provider-unavailable",
                        "_toolang__exec",
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
                "_toolang__chdir",
                "_toolang__exec",
                "_toolang__pick",
                "_toolang__run",
                "_toolang__spawn",
                "_toolang__await",
            }
            assert route_snapshots(first_call) == {"hands": [], "handoffs": []}
            assert '"runnables"' not in first_call.instructions
            prohibitions = first_call.instructions.split("## Don't", 1)[1]
            assert "call run, spawn, or exec without authorized routes" in prohibitions
            result = last_tool_result(harness.adapter.invocations[1].call)
            assert isinstance(result, ToolResultPart)
            assert result.error == "Runnable not found: target"

    asyncio.run(scenario())


def test_execute_must_be_the_only_model_tool_call(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller() -> Text:
  recall = none
  handoffs = target
  context = none
  instruct = none
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
                        "_toolang__exec",
                        {"runnable": "target"},
                    ),
                    ToolCall(
                        "second",
                        "provider-second",
                        "_toolang__exec",
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
            assert not harness.store.list_run_controls(run_id=root.id, kind="exec")
            results = tuple(
                part
                for message in harness.adapter.invocations[1].call.messages
                for part in message.parts
                if isinstance(part, ToolResultPart)
            )
            assert len(results) == 2
            assert all(
                isinstance(item, ToolResultPart)
                and item.error
                == "_toolang/exec must be the only tool call in its Model Call"
                for item in results
            )

    asyncio.run(scenario())


def test_exec_can_return_to_a_replaced_runnable(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller() -> Text:
  recall = none
  handoffs = target
  context = none
  instruct = none
  Call.

agic target() -> Text:
  recall = none
  handoffs = caller
  context = none
  instruct = none
  Target.
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "to-target",
                        "provider-target",
                        "_toolang__exec",
                        {"runnable": "target"},
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "to-caller",
                        "provider-caller",
                        "_toolang__exec",
                        {"runnable": "caller"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("caller completed")),
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
                ("tool", "succeeded"),
                ("model", "succeeded"),
            ]
            controls = harness.store.list_run_controls(run_id=root.id, kind="exec")
            assert len(controls) == 2
            assert [
                control.payload.runnable
                for control in controls
                if isinstance(control.payload, ExecControlPayload)
            ] == ["agic:target", "agic:caller"]
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            for invocation, expected in zip(
                harness.adapter.invocations,
                ["agic:target", "agic:caller", "agic:target"],
                strict=True,
            ):
                assert [
                    route["ref"]
                    for route in route_snapshots(invocation.call)["handoffs"]
                ] == [expected]
            assert "Call." in str(harness.adapter.invocations[2].call.messages)

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
  context = none
  instruct = none
  Call.

agic middle(_: Text) -> Text:
  recall = none
  handoffs = flow:deliver
  context = none
  instruct = none
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
                        "_toolang__exec",
                        {"runnable": "middle", "input": {"_": "work"}},
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "to-deliver",
                        "provider-deliver",
                        "_toolang__exec",
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
            controls = harness.store.list_run_controls(run_id=root.id, kind="exec")
            assert [
                control.payload.runnable
                for control in controls
                if isinstance(control.payload, ExecControlPayload)
            ] == [
                "agic:middle",
                "flow:deliver",
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
  context = none
  instruct = none
  Inner.
""",
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "run-ancestor",
                        "provider-ancestor",
                        "_toolang__run",
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
                ("tool", "failed"),
                ("model", "succeeded"),
            ]
            result = last_tool_result(harness.adapter.invocations[1].call)
            assert isinstance(result, ToolResultPart)
            assert result.error == (
                "_toolang/run cannot call the current or an ancestor runnable: flow:outer"
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
                        "_toolang__exec",
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
            controls = harness.store.list_run_controls(run_id=root.id, kind="exec")
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
                        "_toolang__exec",
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


def test_dynamic_public_agic_keeps_its_resource_scope_after_publication(
    tmp_path: Path,
) -> None:
    source = """instruct target_instruct:
  old target state
  bound route {{runnable.name}}


flow outer(_: Text) -> Text:
  run caller

agic caller(_: Text) -> Text:
  recall = none
  hands = agic:target
  context = none
  instruct = none
  user: {{_}}

agic target(_: Text) -> Text:
  recall = none
  tools = beta/*
  context = none
  instruct = target_instruct
  user: {{_}}
"""
    target = "target"
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
        tools=tools,
        responses=(
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="run-public-target",
                        call_id="provider-run-public-target",
                        name="_toolang__run",
                        input={
                            "runnable": f"agic:{target}",
                            "input": {"_": "topic"},
                        },
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="publication-public-target",
                        call_id="provider-publication-public-target",
                        name="_toolang__chdir",
                        input={"path": "lab://"},
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
        tracer = PublicationTracer(
            harness, {"publication-public-target": await watcher.refresh()}
        )
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:outer",
                    primary=resolve_input_parts("start"),
                ),
                tracer=tracer,
            )

            assert root.status == "succeeded", root.error
            before_publication = harness.adapter.invocations[1].call
            after_publication = harness.adapter.invocations[2].call
            assert {tool.name for tool in before_publication.tools} == {
                "_toolang__chdir",
                "_toolang__exec",
                "_toolang__pick",
                "_toolang__run",
                "_toolang__spawn",
                "_toolang__await",
                "beta__use",
            }
            assert {tool.name for tool in after_publication.tools} == {
                "_toolang__chdir",
                "_toolang__exec",
                "_toolang__pick",
                "_toolang__run",
                "_toolang__spawn",
                "_toolang__await",
                "beta__use",
            }
            assert "old target state" in before_publication.instructions
            assert "new target state" in after_publication.instructions
            assert f"bound route {target}" in before_publication.instructions
            assert f"bound route {target}" in after_publication.instructions

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "operation,directive", [("run", "hands"), ("exec", "handoffs")]
)
@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("selection", [None, "*", "target", "other", "none"])
def test_named_requests_follow_effective_scope(
    tmp_path: Path,
    operation: Literal["run", "exec"],
    directive: str,
    kind: str,
    selection: str | None,
) -> None:
    other = "handoffs" if directive == "hands" else "hands"
    setting = f"  {directive} = {selection}\n" if selection else ""
    body = (
        "  user: {{_}} {{count}}\n"
        if kind == "agic"
        else "  let result =\n    {{_}} {{count}}\n"
    )
    follow_up = ", then summarize its result" if operation == "run" else ""
    source = f"""
agic caller() -> Text:
  recall = none
  context = none
  {other} = none
{setting}  user: Call {kind}:target with payload and count 3{follow_up}.

{kind} target(_: Text, count: Number) -> Text:
  context = none
  hands = none
  handoffs = none
{body}
agic other() -> Text:
  user: Other.
"""
    target_output = "payload 3" if kind == "agic" else "payload"
    allowed = selection in {None, "*", "target"}
    responses = [
        ModelCallResult(
            tool_calls=(
                ToolCall(
                    "named",
                    "named",
                    f"_toolang__{operation}",
                    {
                        "runnable": f"{kind}:target",
                        "input": {"_": "payload", "count": 3},
                    },
                ),
            )
        ),
    ]
    if allowed and kind == "agic":
        responses.append(ModelCallResult(message=Message.assistant("payload 3")))
    if not allowed or operation == "run":
        responses.append(
            ModelCallResult(
                message=Message.assistant(
                    f"Summary: {target_output}"
                    if allowed
                    else "The requested target is outside the scope."
                )
            )
        )
    harness = ExecutionHarness.create(tmp_path, source=source, responses=responses)
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:caller"), tracer=tracer
            )
            assert root.status == "succeeded", root.error
            runs = harness.store.list_run_tree(root_run_id=root.id)
            first = harness.adapter.invocations[0].call
            assert {"_toolang__run", "_toolang__exec"} <= {t.name for t in first.tools}
            snapshots = route_snapshots(
                first,
                requested_only={
                    directive: selection is None,
                    other: False,
                },
            )
            assert snapshots[other] == []
            refs = [item["ref"] for item in snapshots[directive]]
            assert (f"{kind}:target" in refs) == allowed
            if allowed:
                target_run = (
                    next(run for run in runs if run.parent)
                    if operation == "run"
                    else root
                )
                (invocation,) = harness.store.list_run_controls(
                    run_id=target_run.id,
                    kind="exec" if operation == "exec" else "run",
                )
                assert isinstance(
                    invocation.payload, RunControlPayload | ExecControlPayload
                )
                assert harness.store.resolve_value(invocation.payload.input) == {
                    "_": "payload",
                    "count": 3,
                }
                target = next(
                    item
                    for item in snapshots[directive]
                    if item["ref"] == f"{kind}:target"
                )
                assert target["input"]["type"] == "Text"
                assert target["parameters"] == [
                    {
                        "documentation": "",
                        "name": "count",
                        "optional": False,
                        "type": "Number",
                    }
                ]
                assert root.output is not None
                assert harness.store.resolve_value(root.output.value) == (
                    f"Summary: {target_output}" if operation == "run" else target_output
                )
                assert len(harness.adapter.invocations) == 1 + (kind == "agic") + (
                    operation == "run"
                )
                if kind == "agic":
                    assert without_runtime_snapshots(
                        harness.adapter.invocations[1].call.messages
                    ) == [Message.user("payload 3")]
                    assert route_snapshots(
                        harness.adapter.invocations[1].call,
                        requested_only={"hands": False, "handoffs": False},
                    ) == {"hands": [], "handoffs": []}
                if operation == "run":
                    resumed = harness.adapter.invocations[-1].call
                    result = last_tool_result(resumed)
                    assert result.error is None
                    assert result.output == {"type": "Text", "value": target_output}
                else:
                    assert (
                        len(
                            harness.store.list_run_controls(run_id=root.id, kind="exec")
                        )
                        == 1
                    )
            else:
                result = last_tool_result(harness.adapter.invocations[-1].call)
                assert (
                    result.error
                    == f"runnable is not authorized by {directive}: {kind}:target"
                )
                assert (
                    harness.store.list_run_controls(run_id=root.id, kind="exec") == ()
                )
            assert len(runs) == (2 if allowed and operation == "run" else 1)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
