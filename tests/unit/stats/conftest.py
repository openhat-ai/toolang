from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

from tests.support.execution_fixtures import project_run_start
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCall, ToolCall
from toolang.execution.store import RunStore
from toolang.execution.types import (
    ModelCost,
    ModelStepGiven,
    ModelStepNoted,
    RunNoted,
    StepRef,
    StepStatus,
    ToolStepGiven,
    ToolStepNoted,
    TokenUsage,
)
from toolang.stats.config import StatsConfig
from toolang.stats.service import StatsService
from toolang.stats.types import timestamp

NOW = 1792326877  # 2026-10-18T12:34:37Z
DAY = 86400


@dataclass
class Fixture:
    source: RunStore
    stats: StatsService
    now: list[int]
    indices: dict[str, int] = field(default_factory=dict)

    def root(
        self,
        name: str,
        start: int,
        *,
        thread: str = "chat_stats",
        parent: StepRef | None = None,
    ) -> str:
        run = f"run_{name}"
        project_run_start(
            self.source,
            run_id=run,
            thread_id=thread,
            origin="chat",
            input=Message.user("fixture"),
            started_at=timestamp(start),
            created_at=timestamp(start),
            parent=parent,
        )
        return run

    def finish(self, run: str, at: int) -> None:
        self.source.finish_run(run_id=run, finished_at=timestamp(at), noted=RunNoted())

    def call(
        self,
        run: str,
        start: int,
        end: int,
        *,
        model: str = "test/model",
        tool: bool = False,
        invoked: bool = True,
        tokens: int = 100,
        cached: int | None = 7,
        spend: float | None = 0.1,
        unknown: bool = False,
        status: StepStatus = "succeeded",
        cost_source: Literal["reported", "estimated", "zero", "unknown"] = "reported",
    ) -> StepRef:
        index = self.indices.get(run, 0)
        self.indices[run] = index + 1
        ref = StepRef.from_local(run, (index,))
        kind = "tool" if tool else "model"
        given = (
            ToolStepGiven(plugin="test", call=ToolCall("call", "call", "test", {}))
            if tool
            else ModelStepGiven(
                setup="fixture",
                model=model,
                call=ModelCall(instructions="", messages=[]),
            )
        )
        self.source.begin_step(
            ref=ref, kind=kind, input=(), given=given, started_at=timestamp(start)
        )
        noted = (
            ToolStepNoted(summary="fixture", invoked=invoked)
            if tool
            else ModelStepNoted(
                invoked=invoked,
                usage=None if unknown else TokenUsage(tokens, tokens // 10, cached),
                cost=ModelCost(
                    amount=spend, currency="USD", source=cost_source, complete=True
                )
                if spend is not None
                else None,
            )
        )
        self.source.finish_step(
            ref=ref,
            kind=kind,
            status=status,
            output=None,
            noted=noted,
            error=None,
            finished_at=timestamp(end),
        )
        return ref

    def refresh(self, cutoff: int | None = None) -> str | None:
        return self.stats.records.refresh(
            cutoff=cutoff if cutoff is not None else self.now[0] - DAY, now=self.now[0]
        )


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch):
    source = RunStore(tmp_path / "runs.db")
    now = [NOW]
    stats = StatsService(
        source.db_path,
        tmp_path / "stats.db",
        config=StatsConfig(),
        clock=lambda: now[0],
    )
    stats.start()
    # Archival is explicitly driven in persistence/query tests, without sleeps.
    monkeypatch.setattr(stats, "request_refresh", lambda: None)
    result = Fixture(source, stats, now)
    assert result.refresh() is None
    yield result
    stats.close()
    source.close()
