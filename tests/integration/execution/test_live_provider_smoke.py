"""Opt-in execution smoke tests backed by a real model provider.

Run these tests explicitly, for example:

    uv run pytest -m live_provider --live-model \
      'deepseek/deepseek-chat'
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from toolang.base.types.policy import RunBindings
from toolang.common.ids import IdIssuer
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.records import ExecControlPayload
from toolang.execution.runnables import parse_runnable_ref, resolve_runnable
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ThreadPrefix
from toolang.lang.input import resolve_input_parts, resolve_runnable_input
from toolang.state.state import AgentState
from toolang.setup import AgentSetup
from tests.support.live_provider import LIVE_PROVIDER_SOURCE, create_live_agent

pytestmark = pytest.mark.live_provider


@pytest.fixture
def live_model(request: pytest.FixtureRequest) -> str:
    """Return the explicitly selected live model or skip this test module."""

    value = request.config.getoption("--live-model")
    if not isinstance(value, str) or not value.strip():
        pytest.skip("pass --live-model to run real-provider smoke tests")
    return value.strip()


@dataclass(slots=True)
class _LiveExecution:
    setup: AgentSetup
    state: AgentState
    store: RunStore
    executor: RunExecutor
    threads: ThreadManager

    @classmethod
    async def create(
        cls,
        root: Path,
        *,
        model: str,
        source: str = LIVE_PROVIDER_SOURCE,
    ) -> _LiveExecution:
        setup, state = await create_live_agent(root, model=model, source=source)
        runtime = setup.layout.runtime
        store = RunStore(runtime / "runs.db")
        ids = IdIssuer(runtime / "ids.json", agent_name=setup.layout.name)

        def load_state(revision: str) -> AgentState:
            assert revision == state.revision
            return state

        return cls(
            setup=setup,
            state=state,
            store=store,
            executor=RunExecutor(
                store,
                ids,
                setup=lambda: setup,
                state=lambda: state,
                load_state=load_state,
            ),
            threads=ThreadManager(store, ids),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.executor.stop()
        self.store.close()

    async def run(
        self, runnable: str, marker: str, *, thread: str | None = None
    ) -> tuple[str, str]:
        thread = thread or self.threads.create(prefix=ThreadPrefix.TERM)
        runnable_name, runnable_kind = parse_runnable_ref(runnable)
        declaration = resolve_runnable(
            self.state.modules["agent"],
            runnable_name,
            kind=runnable_kind,
        )
        selected = self.setup.defaults.model
        assert selected is not None
        record = await asyncio.wait_for(
            self.executor.run(
                RunSpec(
                    setup=self.setup,
                    state=self.state,
                    thread=thread,
                    bindings=RunBindings(runnable=runnable, model=selected.ref),
                    model_request=selected,
                    limits=self.setup.limits,
                    input=resolve_runnable_input(
                        declaration,
                        {"_": resolve_input_parts(marker)},
                        structs={
                            item.name: item
                            for item in self.state.modules["agent"].structs
                        },
                    ),
                )
            ),
            timeout=180,
        )
        assert record.status == "succeeded", record.error
        return record.id, self.store.run_output_text(run_id=record.id)


def test_real_provider_executes_agic(
    tmp_path: Path,
    live_model: str,
) -> None:
    async def scenario() -> None:
        created = await _LiveExecution.create(tmp_path, model=live_model)
        async with created as runtime:
            run_id, output = await runtime.run("smoke", "TOOLANG_AGIC_SMOKE")
            assert "TOOLANG_AGIC_SMOKE" in output
            assert [step.kind for step in runtime.store.list_steps(run_id=run_id)] == [
                "model"
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize("context", ["", "  context = none\n"])
def test_real_provider_executes_requested_agic_once_with_chat_history(
    tmp_path: Path, live_model: str, context: str
) -> None:
    source = (
        "agic:\n  tools = none\n  {{_}}\n\n"
        "## Verify the agent responds, echoing the input with a short confirmation.\n"
        "## @param _ Text to echo back in the confirmation.\n"
        f"agic test(_):\n  tools = none\n{context}"
        "  Confirm the agent is working and echo {{_}}.\n"
    )

    async def scenario() -> None:
        created = await _LiveExecution.create(tmp_path, model=live_model, source=source)
        async with created as runtime:
            runtime.setup = replace(
                runtime.setup,
                limits=replace(runtime.setup.limits, agic_model_calls=5, time=120),
            )
            thread = runtime.threads.create(prefix=ThreadPrefix.TERM)
            await runtime.run(
                "<entry:1>",
                "run agic:test",
                thread=thread,
            )
            run_id, output = await runtime.run(
                "<entry:1>",
                "exec agic:test with input TOOLANG_EXEC_SMOKE",
                thread=thread,
            )
            assert "TOOLANG_EXEC_SMOKE" in output
            (control,) = runtime.store.list_run_controls(run_id=run_id, kind="exec")
            assert isinstance(control.payload, ExecControlPayload)
            assert control.payload.runnable == "agic:test"
            assert len(runtime.store.list_run_tree(root_run_id=run_id)) == 1
            from toolang.execution.types import ToolStepGiven

            steps = runtime.store.list_steps(run_id=run_id)
            tool_calls = [
                s.given.call for s in steps if isinstance(s.given, ToolStepGiven)
            ]
            assert sum(t.name == "_toolang__exec" for t in tool_calls) == 1
            assert all(
                t.name in {"_toolang__runnables", "_toolang__exec"} for t in tool_calls
            )
            assert steps[-1].kind == "model"

    asyncio.run(scenario())


def test_real_provider_executes_flow_with_nested_agic(
    tmp_path: Path,
    live_model: str,
) -> None:
    async def scenario() -> None:
        created = await _LiveExecution.create(tmp_path, model=live_model)
        async with created as runtime:
            run_id, output = await runtime.run("relay", "TOOLANG_FLOW_SMOKE")
            assert "TOOLANG_FLOW_SMOKE" in output
            assert [step.kind for step in runtime.store.list_steps(run_id=run_id)] == [
                "run"
            ]
            children = [
                run
                for run in runtime.store.list_runs(limit=None)
                if runtime.store.root_run_id(run_id=run.id) == run_id
                and run.parent is not None
            ]
            assert len(children) == 1
            assert [
                step.kind for step in runtime.store.list_steps(run_id=children[0].id)
            ] == ["model"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "prompt,target,operation",
    [
        ("Invoke agic:review with input ROUTE_MARKER.", "agic:review", "exec"),
        ("Invoke flow:check with input ROUTE_MARKER.", "flow:check", "exec"),
        ("Run agic:review with input ROUTE_MARKER.", "agic:review", "exec"),
        ("Run flow:check with input ROUTE_MARKER.", "flow:check", "exec"),
        (
            "Invoke agic:review for a smoke test; choose the input yourself.",
            "agic:review",
            "exec",
        ),
        (
            "Run agic:review for a smoke test; choose the input yourself.",
            "agic:review",
            "exec",
        ),
        (
            "Use _toolang__run to invoke agic:review for a smoke test; choose the input yourself and return its result.",
            "agic:review",
            "run",
        ),
        ("What input does agic:review require? Explain only.", "agic:review", None),
        (
            "Choose a suitable worker to review this change list: ROUTE_MARKER. Wait for its result and summarize it.",
            "agic:review",
            "run",
        ),
        (
            "List available runnables and describe their intended use. Do not invoke them.",
            None,
            None,
        ),
    ],
    ids=[
        "named-agic",
        "named-flow",
        "ordinary-run-agic",
        "ordinary-run-flow",
        "chosen-test-input",
        "ordinary-run-chosen-input",
        "explicit-run-input",
        "parameters-only",
        "doc-routing",
        "discovery-only",
    ],
)
def test_real_provider_uses_discovery_for_task_directed_routing(
    tmp_path: Path,
    live_model: str,
    prompt: str,
    target: str | None,
    operation: str | None,
) -> None:
    import json
    from toolang.execution.types import ModelStepNoted, ToolStepGiven

    source = """
agic chat(_: Text) -> Text:
  tools = none
  context = none
  {{_}}

## Review a supplied change list. Echo the supplied text with a short confirmation.
## @param _ The change list to review or test text to echo.
agic review(_: Text) -> Text:
  tools = none
  context = none
  Confirm the review and echo {{_}}.

## Validate a supplied test phrase through the review worker.
flow check(_: Text) -> Text:
  run review
"""

    async def scenario():
        created = await _LiveExecution.create(tmp_path, model=live_model, source=source)
        async with created as runtime:
            runtime.setup = replace(
                runtime.setup,
                limits=replace(runtime.setup.limits, agic_model_calls=6, time=120),
            )
            run_id, output = await runtime.run("chat", prompt)
            runs = runtime.store.list_run_tree(root_run_id=run_id)
            steps = [
                step for run in runs for step in runtime.store.list_steps(run_id=run.id)
            ]
            tools = [s.given.call for s in steps if isinstance(s.given, ToolStepGiven)]
            discoveries = [t for t in tools if t.name == "_toolang__runnables"]
            assert discoveries, output
            if target and operation != "run":
                assert discoveries[0].input.get("name") in {
                    target,
                    target.split(":")[1],
                }
            invocations = [
                t
                for t in tools
                if t.name in {"_toolang__run", "_toolang__exec", "_toolang__spawn"}
            ]
            if operation is None:
                assert invocations == [], output
                assert len(runs) == 1
            else:
                assert target is not None
                assert len(invocations) == 1, invocations
                assert invocations[0].name == f"_toolang__{operation}"
                assert invocations[0].input["runnable"] in {
                    target,
                    target.split(":")[1],
                }
                assert output.strip()
                if "ROUTE_MARKER" in prompt:
                    assert "ROUTE_MARKER" in output
            metrics = []
            for step in steps:
                if isinstance(step.noted, ModelStepNoted):
                    call = runtime.store.rebuild_model_call(step)
                    accounting = step.noted.accounting
                    metrics.append(
                        {
                            "messages": len(call.messages),
                            "input_tokens": accounting.input_tokens
                            if accounting
                            else None,
                            "cached": {
                                m.name: m.quantity
                                for m in accounting.meters
                                if "cache_" in m.name
                            }
                            if accounting
                            else {},
                        }
                    )
            print(
                json.dumps(
                    {
                        "scenario": prompt,
                        "tools": [t.name for t in tools],
                        "calls": metrics,
                        "output": output,
                    },
                    ensure_ascii=False,
                )
            )

    asyncio.run(scenario())
