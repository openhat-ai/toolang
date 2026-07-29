from __future__ import annotations

import asyncio
from io import StringIO

from toolang.base.types.message import TextPart
from toolang.cli.common.execution import ConsoleRunTracer
from toolang.execution.events import RunBegin, RunEnd, RunEvent, StepBegin, StepEnd
from toolang.execution.records import RunControlRef


class _TtyStream(StringIO):
    def isatty(self) -> bool:
        return True


def _render(
    events: list[RunEvent],
    *,
    verbosity: int = 0,
    tty: bool = False,
) -> str:
    stream = _TtyStream() if tty else StringIO()
    tracer = ConsoleRunTracer(
        run_id="run_one",
        verbosity=verbosity,
        stream=stream,
    )

    async def scenario() -> None:
        for event in events:
            await tracer.on_event(event)

    asyncio.run(scenario())
    return stream.getvalue()


def _terminal_lines(output: str) -> list[str]:
    """Apply the small carriage-return/erase subset used by the tracer."""

    lines: list[str] = []
    current = ""
    index = 0
    while index < len(output):
        if output.startswith("\r\x1b[2K", index):
            current = ""
            index += len("\r\x1b[2K")
            continue
        char = output[index]
        if char == "\n":
            lines.append(current)
            current = ""
        elif char != "\r":
            current += char
        index += 1
    if current:
        lines.append(current)
    return lines


def test_console_run_tracer_renders_agic_progress_and_compact_usage() -> None:
    output = _render(
        [
            RunBegin(
                run="run_one",
                input=RunControlRef(),
                context={"runnable": {"kind": "agic", "name": "demo"}},
                started_at="2026-07-26T01:00:00Z",
            ),
            StepBegin(
                step="run_one/0",
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
                started_at="2026-07-26T01:00:00Z",
            ),
            StepEnd(
                step="run_one/0",
                kind="model",
                status="finished",
                noted={
                    "usage": {
                        "input_tokens": 34_000,
                        "output_tokens": 1_500_000,
                    }
                },
                finished_at="2026-07-26T01:00:01.500Z",
            ),
            RunEnd(
                run="run_one",
                status="finished",
                finished_at="2026-07-26T01:00:02Z",
            ),
        ]
    )

    assert output.splitlines() == [
        "→ run run_one · agic:demo",
        "  → step 0 · model deepseek/deepseek-chat",
        "  ✓ step 0 · model deepseek/deepseek-chat · 34k/1.5m tokens · 1.5s",
        "✓ run completed · 2.0s",
    ]


def test_console_run_tracer_reports_one_failure_diagnostic() -> None:
    output = _render(
        [
            RunBegin(
                run="run_one",
                input=RunControlRef(),
                context={"runnable": {"kind": "agic", "name": "demo"}},
            ),
            StepBegin(
                step="run_one/0",
                kind="tool",
                given={"tool": "shell"},
            ),
            StepEnd(
                step="run_one/0",
                kind="tool",
                status="failed",
                error="boom",
            ),
            RunEnd(run="run_one", status="failed", error="boom"),
        ],
        verbosity=1,
    )

    assert output.splitlines() == [
        "→ run run_one · agic:demo",
        "  → step 0 · tool shell",
        "  ✗ step 0 · tool shell: boom",
        "✗ run failed",
    ]
    assert output.count("boom") == 1


def test_console_run_tracer_uses_flow_docs_and_aggregates_parallel_items() -> None:
    output = _render(
        [
            RunBegin(
                run="run_one",
                input=RunControlRef(),
                context={"runnable": {"kind": "flow", "name": "research"}},
                started_at="2026-07-26T01:00:00Z",
            ),
            StepBegin(
                step="run_one/0",
                kind="run",
                given={
                    "doc": "Expand the research question",
                    "statement": "scatter",
                    "count": 2,
                    "runnable": "expand_queries",
                    "source": {"line": 45},
                },
                started_at="2026-07-26T01:00:00Z",
            ),
            StepEnd(
                step="run_one/0",
                kind="run",
                status="finished",
                noted={"shape": "list", "items": 2},
                finished_at="2026-07-26T01:00:01Z",
            ),
            StepBegin(
                step="run_one/1",
                kind="par",
                given={
                    "doc": "Search the web",
                    "statement": "map",
                    "runnable": "search_web",
                    "par": 2,
                    "source": {"line": 47},
                },
                started_at="2026-07-26T01:00:01Z",
            ),
            RunBegin(
                run="run_child_a",
                parent="run_one/1",
                input=RunControlRef(),
                context={
                    "runnable": {"kind": "agic", "name": "search_web"},
                    "placement": {"item": 0, "items": 2, "lane": 0, "lanes": 2},
                },
            ),
            RunBegin(
                run="run_child_b",
                parent="run_one/1",
                input=RunControlRef(),
                context={
                    "runnable": {"kind": "agic", "name": "search_web"},
                    "placement": {"item": 1, "items": 2, "lane": 1, "lanes": 2},
                },
            ),
            RunEnd(run="run_child_b", status="finished"),
            RunEnd(run="run_child_a", status="finished"),
            StepEnd(
                step="run_one/1",
                kind="par",
                status="finished",
                noted={"shape": "list", "items": 2},
                finished_at="2026-07-26T01:00:03Z",
            ),
            RunEnd(
                run="run_one",
                status="finished",
                finished_at="2026-07-26T01:00:03Z",
            ),
        ],
        verbosity=3,
    )

    assert output.splitlines() == [
        "→ run run_one · flow:research",
        "  → step 0 · Expand the research question · "
        "scatter 2 expand_queries · line 45",
        "  ✓ step 0 · Expand the research question · "
        "2 items · line 45 · 1.0s",
        "  → step 1 · Search the web · map search_web par 2 · line 47",
        "  ✓ step 1 · Search the web · 2/2 items · line 47 · 2.0s",
        "✓ run completed · 3.0s",
    ]
    assert "run_child" not in output
    assert "item 1/2" not in output


def test_console_run_tracer_keeps_non_parallel_flow_children_folded() -> None:
    output = _render(
        [
            RunBegin(
                run="run_one",
                input=RunControlRef(),
                context={"runnable": {"kind": "flow", "name": "research"}},
            ),
            StepBegin(
                step="run_one/0",
                kind="run",
                given={
                    "doc": "Expand the research question",
                    "statement": "scatter",
                    "count": 6,
                    "runnable": "expand_queries",
                },
            ),
            RunBegin(
                run="run_child",
                parent="run_one/0",
                input=RunControlRef(),
                context={
                    "runnable": {
                        "kind": "agic",
                        "name": "expand_queries",
                    }
                },
            ),
            StepBegin(
                step="run_child/0",
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            StepEnd(
                step="run_child/0",
                kind="model",
                status="finished",
                noted={
                    "usage": {
                        "input_tokens": 4_600,
                        "output_tokens": 63,
                    }
                },
                output=(TextPart('["one", "two"]'),),
            ),
            RunEnd(run="run_child", status="finished"),
            StepEnd(
                step="run_one/0",
                kind="run",
                status="finished",
                noted={"items": 6},
            ),
            RunEnd(run="run_one", status="finished"),
        ],
        verbosity=3,
    )

    assert output.splitlines() == [
        "→ run run_one · flow:research",
        "  → step 0 · Expand the research question · "
        "scatter 6 expand_queries",
        "  ✓ step 0 · Expand the research question · 6 items",
        "✓ run completed",
    ]
    assert "run_child" not in output
    assert "deepseek" not in output
    assert 'output: "[' not in output


def test_console_run_tracer_shows_one_failed_parallel_item() -> None:
    model_output = (
        "A relevance score of 10 requires extensive analysis. "
        "The available evidence is useful, but incomplete. Score: 8"
    )
    output = _render(
        [
            RunBegin(
                run="run_one",
                input=RunControlRef(),
                context={"runnable": {"kind": "flow", "name": "research"}},
            ),
            StepBegin(
                step="run_one/3",
                kind="par",
                given={
                    "doc": "Rank the remaining evidence",
                    "statement": "rank",
                    "limit": "top",
                    "count": 8,
                    "source": {"line": 51},
                },
            ),
            RunBegin(
                run="run_bad",
                parent="run_one/3",
                input=RunControlRef(),
                context={
                    "runnable": {"kind": "agic", "name": "<agic:51>"},
                    "placement": {"item": 2, "items": 5, "lane": 2, "lanes": 5},
                },
            ),
            StepBegin(
                step="run_bad/0",
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            StepEnd(
                step="run_bad/0",
                kind="model",
                status="finished",
                output=(TextPart(model_output),),
            ),
            StepBegin(
                step="run_bad/1",
                kind="system",
                given={"runtime": "failure"},
            ),
            StepEnd(
                step="run_bad/1",
                kind="system",
                status="failed",
                error="output is not valid Number",
            ),
            RunEnd(
                run="run_bad",
                status="failed",
                error="output is not valid Number",
            ),
            StepEnd(
                step="run_one/3",
                kind="par",
                status="failed",
                error="output is not valid Number",
            ),
            RunEnd(
                run="run_one",
                status="failed",
                error="output is not valid Number",
            ),
        ],
        verbosity=3,
    )

    assert (
        "✗ step 3 · Rank the remaining evidence · "
        "item 3/5 · run_bad · line 51: "
        "output is not valid Number"
    ) in output
    assert f'    output: "{model_output}"' in output
    assert output.count("output is not valid Number") == 1
    assert "→ agic:<agic:51>" not in output


def test_console_run_tracer_uses_one_live_lane_summary() -> None:
    output = _render(
        [
            RunBegin(
                run="run_one",
                input=RunControlRef(),
                context={"runnable": {"kind": "flow", "name": "research"}},
            ),
            StepBegin(
                step="run_one/1",
                kind="par",
                given={"doc": "Search the web", "statement": "map"},
            ),
            RunBegin(
                run="run_a",
                parent="run_one/1",
                input=RunControlRef(),
                context={
                    "runnable": {"kind": "agic", "name": "search"},
                    "placement": {"item": 4, "items": 100, "lane": 0, "lanes": 2},
                },
            ),
            RunBegin(
                run="run_b",
                parent="run_one/1",
                input=RunControlRef(),
                context={
                    "runnable": {"kind": "agic", "name": "search"},
                    "placement": {"item": 5, "items": 100, "lane": 1, "lanes": 2},
                },
            ),
            RunEnd(run="run_a", status="finished"),
            RunEnd(run="run_b", status="finished"),
            StepEnd(
                step="run_one/1",
                kind="par",
                status="finished",
                noted={"items": 100},
            ),
            RunEnd(run="run_one", status="finished"),
        ],
        verbosity=3,
        tty=True,
    )

    assert "… step 1 · Search the web · 0/100 · L1→5 L2→6" in output
    assert "… step 1 · Search the web · 1/100 · L2→6" in output
    assert "✓ step 1 · Search the web · 2/100 items" in output
    assert "→ step 1" not in output
    assert output.count("✓ step 1") == 1
    assert "run_a" not in output
    assert "run_b" not in output
    assert _terminal_lines(output) == [
        "→ run run_one · flow:research",
        "  ✓ step 1 · Search the web · 2/100 items",
        "✓ run completed",
    ]
