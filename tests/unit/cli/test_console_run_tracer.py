from __future__ import annotations

import asyncio
from collections.abc import Mapping
from io import StringIO

from toolang.base.types.message import TextDelta, TextPart, ToolResultPart
from toolang.cli.common.script_progress import ConsoleRunTracer
from toolang.execution.events import (
    PartDelta,
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
)
from toolang.execution.records import OutputRef, RunControlRef


class _TtyStream(StringIO):
    def isatty(self) -> bool:
        return True


def _render(
    events: list[RunEvent],
    *,
    verbosity: int = 0,
    tty: bool = False,
    kind: str | None = None,
    name: str | None = None,
    doc: str | None = None,
    input_text: str | None = None,
    args: Mapping[str, object] | None = None,
    width: int | None = None,
) -> str:
    stream = _TtyStream() if tty else StringIO()
    tracer = ConsoleRunTracer(
        run_id="run_one",
        verbosity=verbosity,
        stream=stream,
        runnable_kind=kind,
        runnable_name=name,
        runnable_doc=doc,
        input_value=(TextPart(input_text),) if input_text is not None else (),
        args=args,
        width=width,
    )

    async def scenario() -> None:
        for event in events:
            await tracer.on_event(event)
        tracer.close()

    asyncio.run(scenario())
    return stream.getvalue()


def test_default_root_block_omits_optional_input() -> None:
    output = _render(
        [
            _agic_begin(),
            RunEnd(run="run_one", status="finished"),
        ],
        input_text="hello world",
        args={"count": 2},
    )

    assert output.splitlines() == [
        "Run agic demo",
        "",
        "--- run_one succeeded ---",
        "-------------------------",
    ]


def test_verbose_root_block_aligns_description_and_input_paragraphs() -> None:
    output = _render(
        [
            RunBegin(
                run="run_one",
                input=RunControlRef(index=3),
                context={"runnable": {"kind": "agic", "name": "demo"}},
            ),
            RunEnd(run="run_one", status="finished"),
        ],
        verbosity=2,
        doc="Run the documented demo.",
        input_text="hello world",
        args={"count": 2},
    )

    assert output.splitlines() == [
        "Run agic demo",
        "Run the documented demo.",
        "",
        "> hello world",
        "  count=2",
        "  run_one@3",
        "",
        "--- run_one succeeded ---",
        "-------------------------",
    ]


def test_model_preview_and_facts_follow_verbosity() -> None:
    events: list[RunEvent] = [
        _agic_begin(started_at="2026-07-26T01:00:00Z"),
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
            output=(TextPart("A concise answer."),),
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
            output=OutputRef(step="run_one/0"),
            finished_at="2026-07-26T01:00:02Z",
        ),
    ]

    default = _render(events)
    detailed = _render(events, verbosity=1)
    complete = _render(events, verbosity=2)

    assert "A concise answer." not in default
    assert "· A concise answer." in detailed
    assert "run_one/0" not in detailed
    assert (
        "run_one/0 · 1.5s · deepseek/deepseek-chat · 34k/1.5m tokens"
        in complete
    )
    assert complete.splitlines()[-4:] == [
        "--- run_one succeeded ---",
        "1 item returned",
        "2.0s · 34k/1.5m tokens · 1 model call",
        "-------------------------",
    ]


def test_step_output_wraps_at_the_content_boundary() -> None:
    output = _render(
        [
            _agic_begin(),
            StepBegin(
                step="run_one/0",
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            StepEnd(
                step="run_one/0",
                kind="model",
                status="finished",
                output=(
                    TextPart(
                        "Alpha beta gamma delta epsilon zeta eta theta iota kappa."
                    ),
                ),
            ),
            RunEnd(run="run_one", status="finished"),
        ],
        verbosity=1,
        width=40,
    )

    content = [
        line
        for line in output.splitlines()
        if "Alpha" in line or "eta theta" in line
    ]
    assert content == [
        "· Alpha beta gamma delta epsilon zeta",
        "  eta theta iota kappa.",
    ]
    assert all(len(line) <= 40 for line in content)


def test_tool_failure_uses_output_then_facts_and_is_not_repeated() -> None:
    output = _render(
        [
            _agic_begin(started_at="2026-07-26T01:00:00Z"),
            StepBegin(
                step="run_one/0",
                kind="tool",
                given={"tool": "web_search.search"},
                started_at="2026-07-26T01:00:00Z",
            ),
            StepEnd(
                step="run_one/0",
                kind="tool",
                status="failed",
                error="provider returned status 429",
                finished_at="2026-07-26T01:00:00.820Z",
            ),
            RunEnd(
                run="run_one",
                status="failed",
                error="provider returned status 429",
                finished_at="2026-07-26T01:00:00.820Z",
            ),
        ],
    )

    assert output.splitlines() == [
        "Run agic demo",
        "",
        "! web_search.search: provider returned status 429",
        "  run_one/0 · 820ms · exit 429",
        "",
        "--- run_one failed ---",
        "820ms · 1 tool call",
        "----------------------",
    ]
    assert output.count("provider returned status 429") == 1


def test_tool_success_uses_result_then_complete_facts() -> None:
    output = _render(
        [
            _agic_begin(),
            StepBegin(
                step="run_one/0",
                kind="tool",
                given={"tool": "shell.execute"},
                started_at="2026-07-26T01:00:00Z",
            ),
            StepEnd(
                step="run_one/0",
                kind="tool",
                status="finished",
                output=(
                    ToolResultPart(
                        tool_call_id="call_1",
                        tool_name="shell.execute",
                        tool_family="shell",
                        output={"exit_code": 0},
                    ),
                ),
                finished_at="2026-07-26T01:00:00.050Z",
            ),
            RunEnd(run="run_one", status="finished"),
        ],
        verbosity=2,
    )

    assert "· shell.execute: exit 0" in output
    assert "  run_one/0 · 50ms · exit 0" in output


def test_tty_dims_progress_but_not_live_or_errors() -> None:
    active = _render(
        [
            _agic_begin(),
            StepBegin(
                step="run_one/0",
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
        ],
        tty=True,
    )
    failed = _render(
        [
            _agic_begin(),
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
        tty=True,
    )

    assert "\x1b[2mRun agic demo\x1b[0m" in active
    assert "· thinking…" in active
    assert "\x1b[2m· thinking…" not in active
    assert "\x1b[31m! shell: boom\x1b[0m" in failed
    assert "\x1b[2m! shell: boom\x1b[0m" not in failed


def test_flow_statement_uses_zero_based_index_and_natural_work_sentence() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step="run_one/0",
                kind="run",
                given={
                    "statement": "run",
                    "runnable": "review",
                    "binding": "report",
                    "doc": "Review the draft.",
                    "source": {"line": 40},
                },
            ),
            RunBegin(
                run="run_review",
                parent="run_one/0",
                input=RunControlRef(),
                context={"runnable": {"kind": "agic", "name": "review"}},
            ),
            RunEnd(run="run_review", status="finished"),
            StepEnd(
                step="run_one/0",
                kind="run",
                status="finished",
                output=(TextPart("Revised report."),),
                noted={"shape": "item"},
            ),
            RunEnd(
                run="run_one",
                status="finished",
                output=OutputRef(step="run_one/0"),
            ),
        ],
        verbosity=2,
    )

    assert "[0] run review" in output
    assert "[0] run review ·" not in output
    assert "line 40" not in output
    assert "  Review the draft." in output
    assert "  Run agic review" in output
    assert "run_review succeeded" not in output
    assert "  Save result to report" in output
    assert "  · 1 item · returned by one run" in output


def test_scatter_keeps_work_and_semantic_result_separate() -> None:
    output = _render(
        [
            _flow_begin(started_at="2026-07-26T01:00:00Z"),
            StepBegin(
                step="run_one/0",
                kind="run",
                given={
                    "statement": "scatter",
                    "count": 6,
                    "runnable": "expand_queries",
                    "binding": "_",
                },
                started_at="2026-07-26T01:00:00Z",
            ),
            RunBegin(
                run="run_queries",
                parent="run_one/0",
                input=RunControlRef(),
                context={
                    "runnable": {"kind": "agic", "name": "expand_queries"}
                },
            ),
            RunEnd(run="run_queries", status="finished"),
            StepEnd(
                step="run_one/0",
                kind="run",
                status="finished",
                noted={"shape": "list", "items": 6},
                finished_at="2026-07-26T01:00:02Z",
            ),
            RunEnd(
                run="run_one",
                status="finished",
                output=OutputRef(step="run_one/0"),
                finished_at="2026-07-26T01:00:02Z",
            ),
        ],
        verbosity=2,
    )

    assert "  Run agic expand_queries" in output
    assert "  Save result to _" in output
    assert "  · 6-item list · scattered from 1 item" in output
    assert "~~~" not in output
    assert "= " not in output


def test_scatter_transform_failure_has_one_actionable_boundary() -> None:
    output = _render(
        [
            _flow_begin(started_at="2026-07-26T01:00:00Z"),
            StepBegin(
                step="run_one/0",
                kind="run",
                given={
                    "statement": "scatter",
                    "count": 6,
                    "runnable": "expand_queries",
                    "binding": "_",
                },
                started_at="2026-07-26T01:00:00Z",
            ),
            RunBegin(
                run="run_queries",
                parent="run_one/0",
                input=RunControlRef(),
                context={
                    "runnable": {"kind": "agic", "name": "expand_queries"}
                },
            ),
            RunEnd(run="run_queries", status="finished"),
            StepEnd(
                step="run_one/0",
                kind="run",
                status="failed",
                error="scatter requires a list result",
                finished_at="2026-07-26T01:00:01Z",
            ),
            RunEnd(
                run="run_one",
                status="failed",
                error="scatter requires a list result",
                finished_at="2026-07-26T01:00:01Z",
            ),
        ]
    )

    assert "! run_one/0 failed: scatter requires a list result" in output
    assert output.count("scatter requires a list result") == 1
    assert "save result" not in output


def test_parallel_block_is_bounded_and_uses_zero_based_positions() -> None:
    output = _render(
        [
            _flow_begin(started_at="2026-07-26T01:00:00Z"),
            StepBegin(
                step="run_one/2",
                kind="par",
                given={
                    "statement": "rank",
                    "scorer": "relevance",
                    "limit": "top",
                    "count": 8,
                    "par": 2,
                    "binding": "findings",
                },
                started_at="2026-07-26T01:00:00Z",
            ),
            _parallel_run_begin("run_a", item=0, lane=0),
            StepBegin(
                step="run_a/0",
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            _parallel_run_begin("run_b", item=1, lane=1),
            PartDelta(
                step="run_a/0",
                part=0,
                delta=TextDelta("0.82"),
            ),
            RunEnd(run="run_a", status="finished"),
            RunEnd(run="run_b", status="finished"),
            StepEnd(
                step="run_one/2",
                kind="par",
                status="finished",
                noted={"shape": "list", "items": 2},
            ),
            RunEnd(
                run="run_one",
                status="finished",
                output=OutputRef(step="run_one/2"),
            ),
        ],
        tty=True,
    )

    assert "Run agic relevance in parallel (18 items, 2 lanes)" in output
    assert "0 completed · 2 active · 0 failed" in output
    assert "0 │ item 0 | 0.82" in output
    assert "1 │ item 1 | starting…" in output
    assert output.count("Run agic relevance in parallel") == 1


def test_parallel_failure_reports_counts_without_binding() -> None:
    output = _render(
        [
            _flow_begin(started_at="2026-07-26T01:00:00Z"),
            StepBegin(
                step="run_one/2",
                kind="par",
                given={
                    "statement": "rank",
                    "scorer": "relevance",
                    "limit": "top",
                    "count": 8,
                    "par": 4,
                    "binding": "findings",
                },
                started_at="2026-07-26T01:00:00Z",
            ),
            _parallel_run_begin("run_good", item=0, lane=0),
            RunEnd(run="run_good", status="finished"),
            _parallel_run_begin("run_bad", item=5, lane=1),
            RunEnd(
                run="run_bad",
                status="failed",
                error="output is not valid Number",
            ),
            StepEnd(
                step="run_one/2",
                kind="par",
                status="failed",
                error="output is not valid Number",
                finished_at="2026-07-26T01:00:02Z",
            ),
            RunEnd(
                run="run_one",
                status="failed",
                error="output is not valid Number",
                finished_at="2026-07-26T01:00:02Z",
            ),
        ]
    )

    assert "! run_one/2 failed: output is not valid Number" in output
    assert "2.0s · 1 completed · 1 failed" in output
    assert output.count("output is not valid Number") == 1
    assert "Save result to findings" not in output


def test_verbose_parallel_block_leaves_a_stable_work_summary() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step="run_one/1",
                kind="par",
                given={
                    "statement": "map",
                    "runnable": "search_web",
                    "par": 4,
                    "binding": "_",
                },
            ),
            *[
                event
                for item in range(2)
                for event in (
                    _parallel_run_begin(
                        f"run_search_{item}",
                        item=item,
                        lane=item,
                        items=2,
                        lanes=4,
                        runnable="search_web",
                        parent="run_one/1",
                    ),
                    RunEnd(run=f"run_search_{item}", status="finished"),
                )
            ],
            StepEnd(
                step="run_one/1",
                kind="par",
                status="finished",
                noted={"shape": "list", "items": 2},
            ),
            RunEnd(
                run="run_one",
                status="finished",
                output=OutputRef(step="run_one/1"),
            ),
        ],
        verbosity=1,
    )

    assert "  · 2 runs succeeded" in output
    assert "2-item list · mapped" not in output
    assert "save result" not in output


def test_one_item_and_one_item_list_are_distinct() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step="run_one/0",
                kind="par",
                given={
                    "statement": "map",
                    "runnable": "normalize",
                    "binding": "normalized",
                },
            ),
            _parallel_run_begin(
                "run_normalize",
                item=0,
                lane=0,
                items=1,
                lanes=1,
                runnable="normalize",
                parent="run_one/0",
            ),
            RunEnd(run="run_normalize", status="finished"),
            StepEnd(
                step="run_one/0",
                kind="par",
                status="finished",
                noted={"shape": "list", "items": 1},
            ),
            StepBegin(
                step="run_one/1",
                kind="run",
                given={
                    "statement": "gather",
                    "runnable": "synthesize",
                    "binding": "report",
                },
            ),
            RunBegin(
                run="run_report",
                parent="run_one/1",
                input=RunControlRef(),
                context={"runnable": {"kind": "agic", "name": "synthesize"}},
            ),
            RunEnd(run="run_report", status="finished"),
            StepEnd(
                step="run_one/1",
                kind="run",
                status="finished",
                output=(TextPart("report"),),
                noted={"shape": "item"},
            ),
            RunEnd(
                run="run_one",
                status="finished",
                output=OutputRef(step="run_one/1"),
            ),
        ],
        verbosity=2,
    )

    assert "· 1-item list · mapped from a 1-item list" in output
    assert "· 1 item · gathered from a list" in output


def test_empty_parallel_statements_keep_work_and_actual_result_counts() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step="run_one/2",
                kind="par",
                given={
                    "statement": "keep",
                    "predicate": "is_relevant",
                    "binding": "_",
                },
            ),
            *[
                event
                for item in range(6)
                for event in (
                    _parallel_run_begin(
                        f"run_keep_{item}",
                        item=item,
                        lane=item,
                        items=6,
                        lanes=6,
                        runnable="is_relevant",
                        parent="run_one/2",
                    ),
                    RunEnd(run=f"run_keep_{item}", status="finished"),
                )
            ],
            StepEnd(
                step="run_one/2",
                kind="par",
                status="finished",
                noted={"shape": "list", "items": 0},
            ),
            StepBegin(
                step="run_one/3",
                kind="par",
                given={
                    "statement": "rank",
                    "scorer": "<agic:51>",
                    "limit": "top",
                    "count": 8,
                    "binding": "_",
                },
            ),
            StepEnd(
                step="run_one/3",
                kind="par",
                status="finished",
                noted={"shape": "list", "items": 0},
            ),
            StepBegin(
                step="run_one/4",
                kind="par",
                given={
                    "statement": "map",
                    "runnable": "extract_findings",
                    "par": 4,
                    "binding": "_",
                },
            ),
            StepEnd(
                step="run_one/4",
                kind="par",
                status="finished",
                noted={"shape": "list", "items": 0},
            ),
            RunEnd(
                run="run_one",
                status="finished",
                output=OutputRef(step="run_one/4"),
            ),
        ],
        verbosity=2,
    )

    assert output.count("  Save result to _") == 3
    assert "· 0-item list · kept 0 of 6 items" in output
    assert "  · 6 runs succeeded" in output
    assert "  Run agic <agic:L51> in parallel (0 items)" in output
    assert output.count("  · 0 runs · empty input list") == 2
    assert "· 0-item list · selected top 0 of 0 items" in output
    assert "  Run extract_findings in parallel (0 items, 4 lanes)" in output
    assert "· 0-item list · mapped from a 0-item list" in output


def test_default_flow_keeps_headers_and_work_lines_compact() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step="run_one/0",
                kind="run",
                given={
                    "statement": "scatter",
                    "count": 6,
                    "runnable": "expand_queries",
                    "binding": "_",
                },
            ),
            RunBegin(
                run="run_expand",
                parent="run_one/0",
                input=RunControlRef(),
                context={
                    "runnable": {"kind": "agic", "name": "expand_queries"}
                },
            ),
            RunEnd(run="run_expand", status="finished"),
            StepEnd(
                step="run_one/0",
                kind="run",
                status="finished",
                noted={"shape": "list", "items": 6},
            ),
            StepBegin(
                step="run_one/1",
                kind="par",
                given={
                    "statement": "rank",
                    "scorer": "<agic:51>",
                    "limit": "top",
                    "count": 8,
                    "binding": "_",
                },
            ),
            StepEnd(
                step="run_one/1",
                kind="par",
                status="finished",
                noted={"shape": "list", "items": 0},
            ),
            RunEnd(
                run="run_one",
                status="finished",
                output=OutputRef(step="run_one/1"),
            ),
        ]
    )

    lines = output.splitlines()
    scatter = lines.index("[0] scatter 6 expand_queries")
    rank = lines.index("[1] rank <agic:L51> · top 8")
    assert lines[scatter + 1] == "  Run agic expand_queries"
    assert lines[rank + 1] == "  Run agic <agic:L51> in parallel (0 items)"


def test_settle_block_shows_one_sequential_work_line_and_latest_item() -> None:
    output = _render(
        [
            _flow_begin(started_at="2026-07-26T01:00:00Z"),
            StepBegin(
                step="run_one/4",
                kind="loop",
                given={
                    "statement": "settle",
                    "runnable": "reducer",
                    "binding": "_",
                },
                started_at="2026-07-26T01:00:00Z",
            ),
            _sequential_run_begin("run_a", item=0),
            RunEnd(run="run_a", status="finished"),
            _sequential_run_begin("run_b", item=1),
            StepBegin(
                step="run_b/0",
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            PartDelta(
                step="run_b/0",
                part=0,
                delta=TextDelta("merged"),
            ),
            StepEnd(
                step="run_b/0",
                kind="model",
                status="finished",
                output=(TextPart("merged"),),
            ),
            RunEnd(run="run_b", status="finished"),
            StepEnd(
                step="run_one/4",
                kind="loop",
                status="finished",
                output=(TextPart("merged"),),
                noted={"shape": "item"},
            ),
            RunEnd(
                run="run_one",
                status="finished",
                output=OutputRef(step="run_one/4"),
            ),
        ],
        tty=True,
    )

    assert "Run agic reducer sequentially (2 items, 2 calls)" in output
    assert "1 call completed · item 1 active" in output
    assert "· merged" in output
    assert output.count("Run agic reducer sequentially") == 1


def test_repeat_block_keeps_nested_iterations_in_the_live_area() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step="run_one/3",
                kind="loop",
                given={
                    "statement": "repeat",
                    "count": 2,
                    "binding": "_",
                },
            ),
            StepBegin(
                step="run_one/3/0",
                kind="run",
                given={
                    "statement": "run",
                    "runnable": "revise",
                    "binding": "_",
                    "placement": {"loop": 0},
                },
            ),
            RunBegin(
                run="run_revise",
                parent="run_one/3/0",
                input=RunControlRef(),
                context={
                    "runnable": {"kind": "agic", "name": "revise"},
                    "placement": {"loop": 0},
                },
            ),
            StepBegin(
                step="run_revise/0",
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            PartDelta(
                step="run_revise/0",
                part=0,
                delta=TextDelta("revising"),
            ),
            StepEnd(
                step="run_revise/0",
                kind="model",
                status="finished",
                output=(TextPart("revised"),),
            ),
            RunEnd(run="run_revise", status="finished"),
            StepEnd(
                step="run_one/3/0",
                kind="run",
                status="finished",
                output=(TextPart("revised"),),
                noted={"shape": "item"},
            ),
            StepEnd(
                step="run_one/3",
                kind="loop",
                status="finished",
                output=(TextPart("revised"),),
                noted={"shape": "item"},
            ),
            RunEnd(
                run="run_one",
                status="finished",
                output=OutputRef(step="run_one/3"),
            ),
        ],
        tty=True,
    )

    assert output.count("[3] repeat 2") == 1
    assert "[0] run revise" not in output
    assert "iteration 0 · 2 total" in output
    assert "· revising" in output


def test_hidden_repeat_step_does_not_consume_its_parent_diagnostic() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step="run_one/3",
                kind="loop",
                given={"statement": "repeat", "count": 2, "binding": "_"},
            ),
            StepBegin(
                step="run_one/3/0",
                kind="run",
                given={
                    "statement": "scatter",
                    "count": 2,
                    "runnable": "expand",
                    "binding": "_",
                    "placement": {"loop": 0},
                },
            ),
            StepEnd(
                step="run_one/3/0",
                kind="run",
                status="failed",
                error="scatter requires a list result",
            ),
            StepEnd(
                step="run_one/3",
                kind="loop",
                status="failed",
                error="scatter requires a list result",
            ),
            RunEnd(
                run="run_one",
                status="failed",
                error="scatter requires a list result",
            ),
        ],
        tty=True,
    )

    assert "! run_one/3 failed: scatter requires a list result" in output
    assert output.count("scatter requires a list result") == 1


def _agic_begin(*, started_at: str = "") -> RunBegin:
    return RunBegin(
        run="run_one",
        input=RunControlRef(),
        context={"runnable": {"kind": "agic", "name": "demo"}},
        started_at=started_at,
    )


def _flow_begin(*, started_at: str = "") -> RunBegin:
    return RunBegin(
        run="run_one",
        input=RunControlRef(),
        context={"runnable": {"kind": "flow", "name": "research"}},
        started_at=started_at,
    )


def _parallel_run_begin(
    run: str,
    *,
    item: int,
    lane: int,
    items: int = 18,
    lanes: int = 2,
    runnable: str = "relevance",
    parent: str = "run_one/2",
) -> RunBegin:
    return RunBegin(
        run=run,
        parent=parent,
        input=RunControlRef(),
        context={
            "runnable": {"kind": "agic", "name": runnable},
            "placement": {
                "item": item,
                "items": items,
                "lane": lane,
                "lanes": lanes,
            },
        },
    )


def _sequential_run_begin(run: str, *, item: int) -> RunBegin:
    return RunBegin(
        run=run,
        parent="run_one/4",
        input=RunControlRef(),
        context={
            "runnable": {"kind": "agic", "name": "reducer"},
            "placement": {
                "item": item,
                "items": 2,
                "loop": item,
            },
        },
    )
