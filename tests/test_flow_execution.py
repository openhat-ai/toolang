from __future__ import annotations

import asyncio
from pathlib import Path

from toolang import agents
from toolang.base.types.message import Message, message_text
from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.base.types.run import ModelCall, ModelCallResult
from toolang.components.trigger import watch
from toolang.execution.db import ExecutionStore, execution_db_path
from toolang.execution.executor import _stage_thunk_with_default_recall
from toolang.execution.model_call import recall_values, recalls_history
from toolang.execution.records import ChildCallStepPayload, FlowOpStepPayload
from toolang.execution.runner import QueueRunner, RunRequest
from toolang.execution.stream import RuntimeEventBus
from toolang.lang.ast import Directive, SourceSpan, Thunk
from toolang.models.config import load_model_aliases
from toolang.state.durable import scan_durable_state
from toolang.state.live import load_live_state
from toolang.up import UptimeConfig, UptimeContext


class _FakeProvider:
    name = "fake"
    description = None

    def required_env_vars(self) -> tuple[str, ...]:
        return ()

    def default_base_url(self, *, environ):
        del environ
        return None

    def default_api_key_env(self):
        return None

    def list_models(self, *, environ):
        del environ
        return (
            ModelInfo(
                ref="gpt-5",
                provider="fake",
                name="Fake",
                model="fake",
                adapter="fake",
                selectors=("gpt-5",),
                tools=False,
                streaming=False,
            ),
        )

    def prepare_target(self, target: ModelTarget) -> ModelTarget:
        return target


class _FakeAdapter:
    name = "fake"
    description = None

    def invoke(self, target: ModelTarget, request: ModelCall) -> ModelCallResult:
        del target
        text = message_text(request.messages[-1].parts) if request.messages else ""
        return ModelCallResult(message=Message.assistant(f"handled {text}".strip()))

    def stream(self, target: ModelTarget, request: ModelCall, *, on_event):
        del on_event
        return self.invoke(target, request)


def test_flow_stage_thunk_defaults_recall_to_none_without_mutating_source() -> None:
    thunk = Thunk(name="search", span=SourceSpan(10))

    stage_thunk = _stage_thunk_with_default_recall(thunk)

    assert stage_thunk is not thunk
    assert recall_values(stage_thunk) == ("none",)
    assert not recalls_history(stage_thunk)
    assert recall_values(thunk) == ()
    assert recalls_history(thunk)


def test_flow_stage_thunk_preserves_explicit_recall_directive() -> None:
    thunk = Thunk(
        name="search",
        directives=(Directive(name="recall", operator="=", values=("history",), span=SourceSpan(11)),),
        span=SourceSpan(10),
    )

    stage_thunk = _stage_thunk_with_default_recall(thunk)

    assert stage_thunk is thunk
    assert recall_values(stage_thunk) == ("history",)
    assert recalls_history(stage_thunk)


def test_program_parse_flow_stages() -> None:
    from toolang.lang.lower import parse

    program = parse(
        "flow review(in: Text):\n"
        "  do summarize\n"
        "  each par 2: Rewrite item\n"
        "  rank 3: Score item\n"
    )

    flow = program.flows[0]
    assert flow.flow_name() == "review"
    assert flow.input is not None
    assert flow.input.name == "in"
    assert [(stage.kind, stage.target, stage.body) for stage in flow.stages] == [
        ("do", "summarize", None),
        ("each", None, "Rewrite item"),
        ("rank", None, "Score item"),
    ]
    assert flow.stages[1].parallelism == 2
    assert flow.stages[2].limit == 3


def test_program_parse_flow_stage_doc_comments() -> None:
    from toolang.lang.lower import parse

    program = parse(
        "flow review(in: Text):\n"
        "  ## Expand query variants\n"
        "  do expand_queries\n"
        "  ## Score evidence\n"
        "  ## Prefer recent source-backed results\n"
        "  rank 3: Score item\n"
    )

    flow = program.flows[0]
    assert flow.stages[0].doc == "Expand query variants"
    assert flow.stages[1].doc == "Score evidence\nPrefer recent source-backed results"


def test_flow_run_records_child_thunk_run(tmp_path: Path) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        source = (
            "agent alice\n\n"
            "thunk summarize(in: Part[]):\n"
            "  Summarize the input.\n\n"
            "flow review(in: Text):\n"
            "  do summarize\n"
        )
        program_path = agents.agent_program_path(toolang_root, "alice")
        program_path.parent.mkdir(parents=True)
        program_path.write_text(source, encoding="utf-8")
        context = _build_context(toolang_root, "alice")
        completion = asyncio.get_running_loop().create_future()
        context.runner.enqueue(
            RunRequest(
                group="chat",
                origin="script",
                thunk_name="review",
                thunk="hello",
                metadata={"executable_kind": "flow"},
            ),
            completion=completion,
        )
        context.runner.close()
        await context.runner.drain(context)
        outcome = completion.result()

        assert outcome.status == "finished"
        parent = context.store.get_run(run_id=outcome.run_id)
        assert parent is not None
        assert parent.executable_kind == "flow"
        runs = context.store.list_runs(limit=None, include_superseded=True)
        child = next(item for item in runs if item.parent_run_id == parent.run_id)
        assert child.executable_kind == "thunk"
        assert child.root_run_id == parent.run_id
        assert child.call_kind == "stage"
        steps = context.store.list_steps(run_id=parent.run_id)
        assert [step.kind for step in steps] == ["step", "run", "bind"]
        assert isinstance(steps[0].payload, FlowOpStepPayload)
        assert steps[0].payload.metadata is not None
        assert steps[0].payload.metadata["stage_label"] == "do summarize"
        assert isinstance(steps[1].payload, ChildCallStepPayload)
        assert steps[1].payload.metadata is not None
        assert steps[1].payload.metadata["stage_label"] == "do summarize"
        assert steps[1].payload.child_run_ids == (child.run_id,)
        assert context.store.list_steps(run_id=child.run_id)[0].kind == "model"

    asyncio.run(run_test())


def test_flow_run_records_inline_stage_child_thunk(tmp_path: Path) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        source = (
            "agent alice\n\n"
            "flow review(in: Text):\n"
            "  do: Rewrite the input.\n"
        )
        program_path = agents.agent_program_path(toolang_root, "alice")
        program_path.parent.mkdir(parents=True)
        program_path.write_text(source, encoding="utf-8")
        context = _build_context(toolang_root, "alice")
        completion = asyncio.get_running_loop().create_future()
        context.runner.enqueue(
            RunRequest(
                group="chat",
                origin="script",
                thunk_name="review",
                thunk="hello",
                metadata={"executable_kind": "flow"},
            ),
            completion=completion,
        )
        context.runner.close()
        await context.runner.drain(context)
        outcome = completion.result()

        assert outcome.status == "finished"
        parent = context.store.get_run(run_id=outcome.run_id)
        assert parent is not None
        runs = context.store.list_runs(limit=None, include_superseded=True)
        child = next(item for item in runs if item.parent_run_id == parent.run_id)
        assert child.executable_kind == "thunk"
        assert child.executable_name is None
        assert child.call_kind == "stage"
        assert child.metadata["child"]["source_line"] == 4
        steps = context.store.list_steps(run_id=parent.run_id)
        assert isinstance(steps[1].payload, ChildCallStepPayload)
        assert steps[1].payload.target_kind == "thunk"
        assert steps[1].payload.target is None
        assert steps[1].payload.metadata is not None
        assert steps[1].payload.metadata["source_line"] == 4
        assert steps[1].payload.child_run_ids == (child.run_id,)
        assert context.store.list_steps(run_id=child.run_id)[0].kind == "model"

    asyncio.run(run_test())


def test_flow_parallel_child_calls_record_lane_metadata(tmp_path: Path) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        source = (
            "agent alice\n\n"
            "flow review(in: Text):\n"
            "  each par 2: Rewrite the input.\n"
        )
        program_path = agents.agent_program_path(toolang_root, "alice")
        program_path.parent.mkdir(parents=True)
        program_path.write_text(source, encoding="utf-8")
        context = _build_context(toolang_root, "alice")
        completion = asyncio.get_running_loop().create_future()
        context.runner.enqueue(
            RunRequest(
                group="chat",
                origin="script",
                thunk_name="review",
                thunk="a\nb\nc",
                metadata={"executable_kind": "flow"},
            ),
            completion=completion,
        )
        context.runner.close()
        await context.runner.drain(context)
        outcome = completion.result()

        assert outcome.status == "finished"
        parent = context.store.get_run(run_id=outcome.run_id)
        assert parent is not None
        child_steps = [
            step
            for step in context.store.list_steps(run_id=parent.run_id)
            if isinstance(step.payload, ChildCallStepPayload)
        ]
        assert len(child_steps) == 3
        first = child_steps[0].payload
        third = child_steps[2].payload
        assert isinstance(first, ChildCallStepPayload)
        assert isinstance(third, ChildCallStepPayload)
        assert first.parallelism == 2
        assert first.lane_index == 0
        assert first.item_indexes == (0,)
        assert first.metadata is not None
        assert first.metadata["parallelism"] == 2
        assert first.metadata["lane_index"] == 0
        assert first.metadata["item_count"] == 3
        assert third.parallelism == 2
        assert third.lane_index in {0, 1}
        assert third.item_indexes == (2,)

    asyncio.run(run_test())


def _build_context(toolang_root: Path, agent_name: str) -> UptimeContext:
    durable = scan_durable_state(toolang_root, agent_name)
    prepared = watch.build_prepared_state(durable)
    live = load_live_state(prepared, enabled_features=("chat",))
    store = ExecutionStore(execution_db_path(toolang_root, agent_name))
    return UptimeContext(
        root=toolang_root,
        name=agent_name,
        live=live,
        tools={},
        model_providers={"fake": _FakeProvider()},
        model_adapters={"fake": _FakeAdapter()},
        model_aliases=load_model_aliases(toolang_root, agent_name),
        default_models=("gpt-5",),
        model_environ={},
        channel_bindings={},
        channel_plugins={},
        runner=QueueRunner(delay_sec=0.0),
        store=store,
        events=RuntimeEventBus(store, agent_id=agent_name),
        config=UptimeConfig(
            {
                "features.enabled": ("chat",),
                "components.enabled": ("runner.chat",),
                "runtime.sandbox": "none",
            }
        ),
    )
