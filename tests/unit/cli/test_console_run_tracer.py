from __future__ import annotations

import asyncio
from io import StringIO

from toolang.cli.common.execution import ConsoleRunTracer
from toolang.execution.events import RunBegin, RunEnd, StepBegin, StepEnd
from toolang.execution.records import RunControlRef


def test_console_run_tracer_renders_compact_ordered_progress() -> None:
    stream = StringIO()
    tracer = ConsoleRunTracer(run_id="run_one", stream=stream)

    async def scenario() -> None:
        await tracer.on_event(
            RunBegin(run="run_one", input=RunControlRef())
        )
        await tracer.on_event(
            StepBegin(step="run_one/0", kind="model")
        )
        await tracer.on_event(
            StepEnd(
                step="run_one/0",
                kind="model",
                status="finished",
            )
        )
        await tracer.on_event(
            RunEnd(run="run_one", status="finished")
        )

    asyncio.run(scenario())

    assert stream.getvalue() == "→ model\n"


def test_console_run_tracer_reports_failures_and_verbose_boundaries() -> None:
    stream = StringIO()
    tracer = ConsoleRunTracer(run_id="run_one", verbosity=1, stream=stream)

    async def scenario() -> None:
        await tracer.on_event(
            RunBegin(run="run_one", input=RunControlRef())
        )
        await tracer.on_event(
            StepBegin(step="run_one/0", kind="tool")
        )
        await tracer.on_event(
            StepEnd(
                step="run_one/0",
                kind="tool",
                status="failed",
                error="boom",
            )
        )
        await tracer.on_event(
            RunEnd(run="run_one", status="failed", error="boom")
        )

    asyncio.run(scenario())

    assert stream.getvalue().splitlines() == [
        "run run_one started",
        "→ tool run_one/0",
        "! tool failed: boom",
        "run failed: boom",
    ]
