"""Opt-in execution smoke tests backed by a real model provider.

Run these tests explicitly, for example:

    uv run pytest -m live_provider --live-model \
      'deepseek/deepseek-chat[deepseek]'
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from toolang.base.types.policy import RunBindings
from toolang.common.ids import IdIssuer
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ThreadPrefix
from toolang.lang.input import perceive_input
from toolang.state.state import AgentState
from toolang.setup import AgentSetup
from tests.support.live_provider import create_live_agent

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
    def create(
        cls,
        root: Path,
        *,
        model: str,
    ) -> _LiveExecution:
        setup, state = create_live_agent(root, model=model)
        runtime = setup.layout.runtime
        store = RunStore(runtime / "runs.db")
        ids = IdIssuer(runtime / "ids.json")
        return cls(
            setup=setup,
            state=state,
            store=store,
            executor=RunExecutor(store, ids),
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
        await self.executor.shutdown()
        self.store.close()

    async def run(self, runnable: str, marker: str) -> tuple[str, str]:
        thread = self.threads.create(prefix=ThreadPrefix.TERM)
        record = await asyncio.wait_for(
            self.executor.start(
                RunSpec(
                    setup=self.setup,
                    state=self.state,
                    thread=thread,
                    bindings=RunBindings(runnable=runnable),
                    limits=self.setup.limits,
                    primary=perceive_input(marker),
                )
            ),
            timeout=180,
        )
        assert record.status == "finished", record.error
        return record.id, self.store.run_output_text(run_id=record.id)


def test_real_provider_executes_agic(
    tmp_path: Path,
    live_model: str,
) -> None:
    async def scenario() -> None:
        async with _LiveExecution.create(tmp_path, model=live_model) as runtime:
            run_id, output = await runtime.run("smoke", "TOOLANG_AGIC_SMOKE")
            assert "TOOLANG_AGIC_SMOKE" in output
            assert [step.kind for step in runtime.store.list_steps(run_id=run_id)] == [
                "model"
            ]

    asyncio.run(scenario())


def test_real_provider_executes_flow_with_nested_agic(
    tmp_path: Path,
    live_model: str,
) -> None:
    async def scenario() -> None:
        async with _LiveExecution.create(tmp_path, model=live_model) as runtime:
            run_id, output = await runtime.run("relay", "TOOLANG_FLOW_SMOKE")
            assert "TOOLANG_FLOW_SMOKE" in output
            assert [step.kind for step in runtime.store.list_steps(run_id=run_id)] == [
                "run"
            ]
            children = [
                run
                for run in runtime.store.list_runs(limit=None)
                if run.root_run_id == run_id and run.parent is not None
            ]
            assert len(children) == 1
            assert [
                step.kind for step in runtime.store.list_steps(run_id=children[0].id)
            ] == ["model"]

    asyncio.run(scenario())
