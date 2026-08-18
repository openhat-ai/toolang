from __future__ import annotations

import asyncio
from collections.abc import Mapping
from io import StringIO

from toolang.base.types.message import Part, TextDelta, TextPart, ToolResultPart
from toolang.cli.common.script_progress import ConsoleRunTracer
from toolang.execution.events import (
    PartDelta,
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
)
from toolang.execution.types import ControlRef, Local, StepPath, Pointer


def _parts(*parts: Part) -> Local:
    return Local.typed("Part[]", tuple(parts), "_", 0)


def _output(step: StepPath) -> Local:
    return Local.typed("Part[]", Pointer.step(step), "_", 0)


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
            RunEnd(run="run_one", status="succeeded"),
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
                control=ControlRef("run_one", 3),
                runnable="agic:demo",
            ),
            RunEnd(run="run_one", status="succeeded"),
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
            step=StepPath.parse("run_one/0"),
            kind="model",
            given={"model": {"ref": "deepseek/deepseek-chat"}},
            started_at="2026-07-26T01:00:00Z",
        ),
        StepEnd(
            step=StepPath.parse("run_one/0"),
            kind="model",
            status="succeeded",
            output=_parts(TextPart("A concise answer.")),
            noted={
                "tokens": {
                    "input": 34_000,
                    "output": 1_500_000,
                }
            },
            finished_at="2026-07-26T01:00:01.500Z",
        ),
        RunEnd(
            run="run_one",
            status="succeeded",
            output=_output(StepPath.parse("run_one/0")),
            finished_at="2026-07-26T01:00:02Z",
        ),
    ]

    default = _render(events)
    detailed = _render(events, verbosity=1)
    complete = _render(events, verbosity=2)

    assert "· A concise answer." in default
    assert "· A concise answer." in detailed
    assert "run_one/0" not in default
    assert "run_one/0" not in detailed
    assert "run_one/0 · 1.5s · deepseek/deepseek-chat · 34k/1.5m tokens" in complete
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
                step=StepPath.parse("run_one/0"),
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            StepEnd(
                step=StepPath.parse("run_one/0"),
                kind="model",
                status="succeeded",
                output=_parts(
                    TextPart(
                        "Alpha beta gamma delta epsilon zeta eta theta iota kappa."
                    ),
                ),
            ),
            RunEnd(run="run_one", status="succeeded"),
        ],
        verbosity=1,
        width=40,
    )

    content = [
        line for line in output.splitlines() if "Alpha" in line or "eta theta" in line
    ]
    assert content == [
        "· Alpha beta gamma delta epsilon zeta",
        "  eta theta iota kappa.",
    ]
    assert all(len(line) <= 40 for line in content)


def test_default_keeps_a_model_step_without_text_output() -> None:
    output = _render(
        [
            _agic_begin(),
            StepBegin(
                step=StepPath.parse("run_one/0"),
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            StepEnd(
                step=StepPath.parse("run_one/0"),
                kind="model",
                status="succeeded",
            ),
            RunEnd(run="run_one", status="succeeded"),
        ]
    )

    assert "· model completed" in output


def test_tool_failure_uses_output_then_facts_and_is_not_repeated() -> None:
    output = _render(
        [
            _agic_begin(started_at="2026-07-26T01:00:00Z"),
            StepBegin(
                step=StepPath.parse("run_one/0"),
                kind="tool",
                given={"tool": "web_search.search"},
                started_at="2026-07-26T01:00:00Z",
            ),
            StepEnd(
                step=StepPath.parse("run_one/0"),
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
    events: list[RunEvent] = [
        _agic_begin(),
        StepBegin(
            step=StepPath.parse("run_one/0"),
            kind="tool",
            given={"tool": "shell.execute"},
            started_at="2026-07-26T01:00:00Z",
        ),
        StepEnd(
            step=StepPath.parse("run_one/0"),
            kind="tool",
            status="succeeded",
            output=_parts(
                ToolResultPart(
                    tool_call_id="call_1",
                    tool_name="shell.execute",
                    tool_family="shell",
                    output={"exit_code": 0},
                ),
            ),
            finished_at="2026-07-26T01:00:00.050Z",
        ),
        RunEnd(run="run_one", status="succeeded"),
    ]
    default = _render(events)
    output = _render(events, verbosity=2)

    assert "· shell.execute: exit 0" in default
    assert "run_one/0" not in default
    assert "· shell.execute: exit 0" in output
    assert "  run_one/0 · 50ms · exit 0" in output


def test_tty_dims_progress_but_not_live_or_errors() -> None:
    active = _render(
        [
            _agic_begin(),
            StepBegin(
                step=StepPath.parse("run_one/0"),
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
                step=StepPath.parse("run_one/0"),
                kind="tool",
                given={"tool": "shell"},
            ),
            StepEnd(
                step=StepPath.parse("run_one/0"),
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
                step=StepPath.parse("run_one/0"),
                kind="run",
                given={
                    "statement": "run",
                    "runnable": "review",
                    "binding": "report",
                    "doc": "Review the draft.",
                    "source": {"line": 40, "head": "let report = run review"},
                },
            ),
            RunBegin(
                run="run_review",
                parent=StepPath.parse("run_one/0"),
                control=ControlRef("run_review", 0),
                runnable="agic:review",
            ),
            RunEnd(run="run_review", status="succeeded"),
            StepEnd(
                step=StepPath.parse("run_one/0"),
                kind="run",
                status="succeeded",
                output=_parts(TextPart("Revised report.")),
                noted={"shape": "item"},
            ),
            RunEnd(
                run="run_one",
                status="succeeded",
                output=_output(StepPath.parse("run_one/0")),
            ),
        ],
        verbosity=2,
    )

    assert "[0] let report = run review" in output
    assert "line 40" not in output
    assert "  Review the draft." in output
    assert "  Run agic review" in output
    assert "  ↳ run_review succeeded" in output
    assert "Save result" not in output
    assert "returned by one run" not in output


def test_scatter_keeps_work_and_semantic_result_separate() -> None:
    output = _render(
        [
            _flow_begin(started_at="2026-07-26T01:00:00Z"),
            StepBegin(
                step=StepPath.parse("run_one/0"),
                kind="run",
                given={
                    "statement": "scatter",
                    "count": 6,
                    "runnable": "expand_queries",
                    "binding": "_",
                    "source": {"head": "scatter 6 expand_queries"},
                },
                started_at="2026-07-26T01:00:00Z",
            ),
            RunBegin(
                run="run_queries",
                parent=StepPath.parse("run_one/0"),
                control=ControlRef("run_queries", 0),
                runnable="agic:expand_queries",
            ),
            RunEnd(run="run_queries", status="succeeded"),
            StepEnd(
                step=StepPath.parse("run_one/0"),
                kind="run",
                status="succeeded",
                noted={"shape": "list", "items": 6},
                finished_at="2026-07-26T01:00:02Z",
            ),
            RunEnd(
                run="run_one",
                status="succeeded",
                output=_output(StepPath.parse("run_one/0")),
                finished_at="2026-07-26T01:00:02Z",
            ),
        ],
        verbosity=2,
    )

    assert "  Run agic expand_queries" in output
    assert "Save result" not in output
    assert "  ↳ run_queries succeeded" in output
    assert "  ↳ 6-item list saved to _ · scattered from 1 item" in output
    assert "~~~" not in output
    assert "= " not in output


def test_statement_spacing_is_compact_at_every_verbosity() -> None:
    events: list[RunEvent] = [
        _flow_begin(),
        StepBegin(
            step=StepPath.parse("run_one/0"),
            kind="system",
            given={
                "statement": "let",
                "binding": "project",
                "source": {"head": "let project"},
            },
        ),
        StepEnd(
            step=StepPath.parse("run_one/0"),
            kind="system",
            status="succeeded",
            output=_parts(TextPart("project")),
            noted={"shape": "item"},
        ),
        StepBegin(
            step=StepPath.parse("run_one/1"),
            kind="run",
            given={
                "statement": "scatter",
                "count": 2,
                "runnable": "decompose",
                "binding": "_",
                "source": {"head": "scatter 2 decompose"},
            },
        ),
        RunBegin(
            run="run_decompose",
            parent=StepPath.parse("run_one/1"),
            control=ControlRef("run_decompose", 0),
            runnable="agic:decompose",
        ),
        RunEnd(run="run_decompose", status="succeeded"),
        StepEnd(
            step=StepPath.parse("run_one/1"),
            kind="run",
            status="succeeded",
            noted={"shape": "list", "items": 2},
        ),
        RunEnd(run="run_one", status="succeeded"),
    ]

    default = _render(events)
    complete = _render(events, verbosity=2)

    assert "[0] let project\n\n[1] scatter 2 decompose" in default
    assert (
        "[1] scatter 2 decompose\n"
        "  Run agic decompose\n"
        "  ↳ 2-item list saved to _ · scattered from 1 item"
    ) in default
    assert (
        "[0] let project\n"
        "  ↳ 1 item saved to project · perceived from authored content\n\n"
        "[1] scatter 2 decompose"
    ) in complete
    assert (
        "  Run agic decompose\n"
        "  ↳ run_decompose succeeded\n"
        "  ↳ 2-item list saved to _ · scattered from 1 item"
    ) in complete


def test_run_failure_keeps_source_header_before_child_acceptance() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step=StepPath.parse("run_one/0"),
                kind="run",
                given={
                    "statement": "run",
                    "runnable": "apply_review",
                    "binding": "_",
                    "source": {"head": "run apply_review"},
                },
            ),
            StepEnd(
                step=StepPath.parse("run_one/0"),
                kind="run",
                status="failed",
                error="missing arguments for apply_review: item",
            ),
            RunEnd(
                run="run_one",
                status="failed",
                error="missing arguments for apply_review: item",
            ),
        ]
    )

    assert "[0] run apply_review" in output
    assert "  Run agic apply_review" not in output
    assert output.count("missing arguments for apply_review: item") == 1


def test_scatter_transform_failure_has_one_actionable_boundary() -> None:
    output = _render(
        [
            _flow_begin(started_at="2026-07-26T01:00:00Z"),
            StepBegin(
                step=StepPath.parse("run_one/0"),
                kind="run",
                given={
                    "statement": "scatter",
                    "count": 6,
                    "runnable": "expand_queries",
                    "binding": "_",
                    "source": {"head": "scatter 6 expand_queries"},
                },
                started_at="2026-07-26T01:00:00Z",
            ),
            RunBegin(
                run="run_queries",
                parent=StepPath.parse("run_one/0"),
                control=ControlRef("run_queries", 0),
                runnable="agic:expand_queries",
            ),
            RunEnd(run="run_queries", status="succeeded"),
            StepEnd(
                step=StepPath.parse("run_one/0"),
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
                step=StepPath.parse("run_one/2"),
                kind="par",
                given={
                    "statement": "rank",
                    "scorer": "relevance",
                    "limit": "top",
                    "count": 8,
                    "par": 2,
                    "binding": "findings",
                    "source": {"head": "let findings = rank relevance top 8 par 2"},
                },
                started_at="2026-07-26T01:00:00Z",
            ),
            _parallel_run_begin("run_a", item=0, lane=0),
            StepBegin(
                step=StepPath.parse("run_a/0"),
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            _parallel_run_begin("run_b", item=1, lane=1),
            PartDelta(
                step=StepPath.parse("run_a/0"),
                part=0,
                delta=TextDelta("0.82"),
            ),
            RunEnd(run="run_a", status="succeeded"),
            RunEnd(run="run_b", status="succeeded"),
            StepEnd(
                step=StepPath.parse("run_one/2"),
                kind="par",
                status="succeeded",
                noted={"shape": "list", "items": 2},
            ),
            RunEnd(
                run="run_one",
                status="succeeded",
                output=_output(StepPath.parse("run_one/2")),
            ),
        ],
        tty=True,
    )

    assert "Run agic relevance in parallel (18 items, 2 lanes)" in output
    assert "· 2 active" in output
    assert "0 │ item 0 | 0.82" in output
    assert "1 │ item 1 | starting…" in output
    assert output.count("Run agic relevance in parallel") == 1


def test_parallel_live_block_keeps_the_final_item_after_lane_reuse() -> None:
    events: list[RunEvent] = [
        _flow_begin(started_at="2026-07-26T01:00:00Z"),
        StepBegin(
            step=StepPath.parse("run_one/2"),
            kind="par",
            given={
                "statement": "map",
                "runnable": "worker",
                "par": 4,
                "binding": "_",
                "source": {"head": "map worker par 4"},
            },
            started_at="2026-07-26T01:00:00Z",
        ),
        *(
            _parallel_run_begin(
                f"run_{item}",
                item=item,
                lane=item,
                items=5,
                lanes=4,
                runnable="worker",
            )
            for item in range(4)
        ),
        RunEnd(run="run_2", status="succeeded"),
        _parallel_run_begin(
            "run_4",
            item=4,
            lane=2,
            items=5,
            lanes=4,
            runnable="worker",
        ),
        RunEnd(run="run_0", status="succeeded"),
        RunEnd(run="run_1", status="succeeded"),
        RunEnd(run="run_3", status="succeeded"),
    ]

    output = _render(events, tty=True)

    assert "· 4 runs succeeded · 1 active" in output
    assert "2 │ item 4 | starting…" in output


def test_parallel_failure_reports_counts_without_a_statement_result() -> None:
    output = _render(
        [
            _flow_begin(started_at="2026-07-26T01:00:00Z"),
            StepBegin(
                step=StepPath.parse("run_one/2"),
                kind="par",
                given={
                    "statement": "rank",
                    "scorer": "relevance",
                    "limit": "top",
                    "count": 8,
                    "par": 4,
                    "binding": "findings",
                    "source": {"head": "let findings = rank relevance top 8 par 4"},
                },
                started_at="2026-07-26T01:00:00Z",
            ),
            _parallel_run_begin("run_good", item=0, lane=0),
            RunEnd(run="run_good", status="succeeded"),
            _parallel_run_begin("run_bad", item=5, lane=1),
            RunEnd(
                run="run_bad",
                status="failed",
                error="output is not valid Number",
            ),
            StepEnd(
                step=StepPath.parse("run_one/2"),
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

    assert "! run_one/2 failed: item 5: output is not valid Number" in output
    assert "· 1 run succeeded · 1 failed · 2.0s" in output
    assert output.count("output is not valid Number") == 1
    assert "[2] let findings = rank relevance top 8 par 4" in output
    assert "↳ top" not in output


def test_parallel_child_failure_is_reported_once_at_the_statement_boundary() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step=StepPath.parse("run_one/0"),
                kind="par",
                given={
                    "statement": "map",
                    "runnable": "search",
                    "par": 2,
                    "binding": "_",
                    "source": {"head": "map search par 2"},
                },
            ),
            _parallel_run_begin(
                "run_search",
                item=2,
                lane=0,
                items=4,
                lanes=2,
                runnable="search",
                parent=StepPath.parse("run_one/0"),
            ),
            StepBegin(
                step=StepPath.parse("run_search/0"),
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            StepEnd(
                step=StepPath.parse("run_search/0"),
                kind="model",
                status="failed",
                error="provider returned status 429",
            ),
            RunEnd(
                run="run_search",
                status="failed",
                error="provider returned status 429",
            ),
            StepEnd(
                step=StepPath.parse("run_one/0"),
                kind="par",
                status="failed",
                error="provider returned status 429",
            ),
            RunEnd(
                run="run_one",
                status="failed",
                error="provider returned status 429",
            ),
        ]
    )

    assert "! run_one/0 failed: item 2: provider returned status 429" in output
    assert output.count("provider returned status 429") == 1
    assert "! deepseek/deepseek-chat" not in output


def test_verbose_parallel_block_leaves_a_stable_work_summary() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step=StepPath.parse("run_one/1"),
                kind="par",
                given={
                    "statement": "map",
                    "runnable": "search_web",
                    "par": 4,
                    "binding": "_",
                    "source": {"head": "map search_web par 4"},
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
                        parent=StepPath.parse("run_one/1"),
                    ),
                    RunEnd(run=f"run_search_{item}", status="succeeded"),
                )
            ],
            StepEnd(
                step=StepPath.parse("run_one/1"),
                kind="par",
                status="succeeded",
                noted={"shape": "list", "items": 2},
            ),
            RunEnd(
                run="run_one",
                status="succeeded",
                output=_output(StepPath.parse("run_one/1")),
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
                step=StepPath.parse("run_one/0"),
                kind="par",
                given={
                    "statement": "map",
                    "runnable": "normalize",
                    "binding": "normalized",
                    "source": {"head": "let normalized = map normalize"},
                },
            ),
            _parallel_run_begin(
                "run_normalize",
                item=0,
                lane=0,
                items=1,
                lanes=1,
                runnable="normalize",
                parent=StepPath.parse("run_one/0"),
            ),
            RunEnd(run="run_normalize", status="succeeded"),
            StepEnd(
                step=StepPath.parse("run_one/0"),
                kind="par",
                status="succeeded",
                noted={"shape": "list", "items": 1},
            ),
            StepBegin(
                step=StepPath.parse("run_one/1"),
                kind="run",
                given={
                    "statement": "gather",
                    "runnable": "synthesize",
                    "binding": "report",
                    "source": {"head": "let report = gather synthesize"},
                },
            ),
            RunBegin(
                run="run_report",
                parent=StepPath.parse("run_one/1"),
                control=ControlRef("run_report", 0),
                runnable="agic:synthesize",
            ),
            RunEnd(run="run_report", status="succeeded"),
            StepEnd(
                step=StepPath.parse("run_one/1"),
                kind="run",
                status="succeeded",
                output=_parts(TextPart("report")),
                noted={"shape": "item"},
            ),
            RunEnd(
                run="run_one",
                status="succeeded",
                output=_output(StepPath.parse("run_one/1")),
            ),
        ],
        verbosity=2,
    )

    assert "↳ 1-item list saved to normalized · mapped from 1 item" in output
    assert "↳ 1 item saved to report · gathered from a list" in output


def test_empty_parallel_statements_keep_work_and_actual_result_counts() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step=StepPath.parse("run_one/2"),
                kind="par",
                given={
                    "statement": "keep",
                    "predicate": "is_relevant",
                    "binding": "_",
                    "source": {"head": "keep is_relevant"},
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
                        parent=StepPath.parse("run_one/2"),
                    ),
                    RunEnd(run=f"run_keep_{item}", status="succeeded"),
                )
            ],
            StepEnd(
                step=StepPath.parse("run_one/2"),
                kind="par",
                status="succeeded",
                noted={"shape": "list", "items": 0},
            ),
            StepBegin(
                step=StepPath.parse("run_one/3"),
                kind="par",
                given={
                    "statement": "rank",
                    "scorer": "<agic:51>",
                    "limit": "top",
                    "count": 8,
                    "binding": "_",
                    "source": {"head": "rank top 8"},
                },
            ),
            StepEnd(
                step=StepPath.parse("run_one/3"),
                kind="par",
                status="succeeded",
                noted={"shape": "list", "items": 0},
            ),
            StepBegin(
                step=StepPath.parse("run_one/4"),
                kind="par",
                given={
                    "statement": "map",
                    "runnable": "extract_findings",
                    "par": 4,
                    "binding": "_",
                    "source": {"head": "map extract_findings par 4"},
                },
            ),
            StepEnd(
                step=StepPath.parse("run_one/4"),
                kind="par",
                status="succeeded",
                noted={"shape": "list", "items": 0},
            ),
            RunEnd(
                run="run_one",
                status="succeeded",
                output=_output(StepPath.parse("run_one/4")),
            ),
        ],
        verbosity=2,
    )

    assert "Save result" not in output
    assert "↳ 0-item list saved to _ · 0/6 items kept" in output
    assert "  · 6 runs succeeded" in output
    assert "  Run agic <agic:L51> in parallel (0 items)" in output
    assert output.count("  · 0 runs · empty input list") == 2
    assert "↳ 0-item list saved to _ · top 0/0 items selected" in output
    assert "  Run agic extract_findings in parallel (0 items, 4 lanes)" in output
    assert "↳ 0-item list saved to _ · mapped from 0 items" in output


def test_default_flow_keeps_headers_and_work_lines_compact() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step=StepPath.parse("run_one/0"),
                kind="run",
                given={
                    "statement": "scatter",
                    "count": 6,
                    "runnable": "expand_queries",
                    "binding": "_",
                    "source": {"head": "scatter 6 expand_queries"},
                },
            ),
            RunBegin(
                run="run_expand",
                parent=StepPath.parse("run_one/0"),
                control=ControlRef("run_expand", 0),
                runnable="agic:expand_queries",
            ),
            RunEnd(run="run_expand", status="succeeded"),
            StepEnd(
                step=StepPath.parse("run_one/0"),
                kind="run",
                status="succeeded",
                noted={"shape": "list", "items": 6},
            ),
            StepBegin(
                step=StepPath.parse("run_one/1"),
                kind="par",
                given={
                    "statement": "rank",
                    "scorer": "<agic:51>",
                    "limit": "top",
                    "count": 8,
                    "binding": "_",
                    "source": {"head": "rank top 8"},
                },
            ),
            StepEnd(
                step=StepPath.parse("run_one/1"),
                kind="par",
                status="succeeded",
                noted={"shape": "list", "items": 0},
            ),
            RunEnd(
                run="run_one",
                status="succeeded",
                output=_output(StepPath.parse("run_one/1")),
            ),
        ]
    )

    lines = output.splitlines()
    scatter = lines.index("[0] scatter 6 expand_queries")
    rank = lines.index("[1] rank top 8")
    assert lines[scatter + 1] == "  Run agic expand_queries"
    assert lines[rank + 1] == "  Run agic <agic:L51> in parallel (0 items)"
    assert "  ↳ 6-item list saved to _ · scattered from 1 item" in lines
    assert "  ↳ 0-item list saved to _ · top 0/0 items selected" in lines


def test_settle_block_shows_one_sequential_work_line_and_latest_item() -> None:
    output = _render(
        [
            _flow_begin(started_at="2026-07-26T01:00:00Z"),
            StepBegin(
                step=StepPath.parse("run_one/4"),
                kind="loop",
                given={
                    "statement": "settle",
                    "runnable": "reducer",
                    "binding": "_",
                    "source": {"head": "settle reducer"},
                },
                started_at="2026-07-26T01:00:00Z",
            ),
            _sequential_run_begin("run_a", item=0),
            RunEnd(run="run_a", status="succeeded"),
            _sequential_run_begin("run_b", item=1),
            StepBegin(
                step=StepPath.parse("run_b/0"),
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            PartDelta(
                step=StepPath.parse("run_b/0"),
                part=0,
                delta=TextDelta("merged"),
            ),
            StepEnd(
                step=StepPath.parse("run_b/0"),
                kind="model",
                status="succeeded",
                output=_parts(TextPart("merged")),
            ),
            RunEnd(run="run_b", status="succeeded"),
            StepEnd(
                step=StepPath.parse("run_one/4"),
                kind="loop",
                status="succeeded",
                output=_parts(TextPart("merged")),
                noted={"shape": "item"},
            ),
            RunEnd(
                run="run_one",
                status="succeeded",
                output=_output(StepPath.parse("run_one/4")),
            ),
        ],
        verbosity=2,
        tty=True,
    )

    assert "Run agic reducer sequentially (2 items)" in output
    assert "· 1 run succeeded · 1 active" in output
    assert "│ item 1 | merged" in output
    assert output.count("Run agic reducer sequentially") == 1
    assert "  ↳ 1 item saved to _ · reduced from 2 items" in output


def test_discard_binding_uses_source_syntax_and_result_wording() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step=StepPath.parse("run_one/0"),
                kind="human",
                given={
                    "statement": "ask",
                    "binding": None,
                    "source": {"head": "let ask"},
                },
            ),
            StepEnd(
                step=StepPath.parse("run_one/0"),
                kind="human",
                status="succeeded",
                output=_parts(TextPart("temporary")),
                noted={"shape": "item"},
            ),
            RunEnd(run="run_one", status="succeeded"),
        ],
        verbosity=2,
    )

    assert "[0] let ask" in output
    assert "  ↳ 1 item discarded · produced" in output


def test_repeat_block_keeps_nested_iterations_in_the_live_area() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step=StepPath.parse("run_one/3"),
                kind="loop",
                given={
                    "statement": "repeat",
                    "count": 2,
                    "binding": "_",
                    "source": {"head": "repeat 2"},
                },
            ),
            StepBegin(
                step=StepPath.parse("run_one/3/0"),
                kind="run",
                given={
                    "statement": "run",
                    "runnable": "revise",
                    "binding": "_",
                    "source": {"head": "run revise"},
                },
                placement={"iter": 0},
            ),
            RunBegin(
                run="run_revise",
                parent=StepPath.parse("run_one/3/0"),
                control=ControlRef("run_revise", 0),
                runnable="agic:revise",
                placement={"iter": 0},
            ),
            StepBegin(
                step=StepPath.parse("run_revise/0"),
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            PartDelta(
                step=StepPath.parse("run_revise/0"),
                part=0,
                delta=TextDelta("revising"),
            ),
            StepEnd(
                step=StepPath.parse("run_revise/0"),
                kind="model",
                status="succeeded",
                output=_parts(TextPart("revised")),
            ),
            RunEnd(run="run_revise", status="succeeded"),
            StepEnd(
                step=StepPath.parse("run_one/3/0"),
                kind="run",
                status="succeeded",
                output=_parts(TextPart("revised")),
                noted={"shape": "item"},
            ),
            StepEnd(
                step=StepPath.parse("run_one/3"),
                kind="loop",
                status="succeeded",
                output=_parts(TextPart("revised")),
                noted={"shape": "item"},
            ),
            RunEnd(
                run="run_one",
                status="succeeded",
                output=_output(StepPath.parse("run_one/3")),
            ),
        ],
        tty=True,
    )

    assert output.count("[3] repeat 2") == 1
    assert "=== iteration 0 ===" in output
    assert "[0] run revise" in output
    assert "Run agic revise" in output
    assert "· revising" in output


def test_hidden_repeat_step_does_not_consume_its_parent_diagnostic() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step=StepPath.parse("run_one/3"),
                kind="loop",
                given={
                    "statement": "repeat",
                    "count": 2,
                    "binding": "_",
                    "source": {"head": "repeat 2"},
                },
            ),
            StepBegin(
                step=StepPath.parse("run_one/3/0"),
                kind="run",
                given={
                    "statement": "scatter",
                    "count": 2,
                    "runnable": "expand",
                    "binding": "_",
                    "source": {"head": "scatter 2 expand"},
                },
                placement={"iter": 0},
            ),
            StepEnd(
                step=StepPath.parse("run_one/3/0"),
                kind="run",
                status="failed",
                error="scatter requires a list result",
            ),
            StepEnd(
                step=StepPath.parse("run_one/3"),
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


def test_complete_repeat_trace_resets_ordinals_and_renders_until_decisions() -> None:
    events: list[RunEvent] = [
        _flow_begin(started_at="2026-07-26T01:00:00Z"),
        StepBegin(
            step=StepPath.parse("run_one/2"),
            kind="loop",
            given={
                "statement": "repeat",
                "count": 3,
                "binding": "_",
                "source": {"head": "repeat 3"},
            },
            started_at="2026-07-26T01:00:00Z",
        ),
    ]
    for iteration, decision in enumerate(("false", "true")):
        body_step = f"run_one/2/{iteration}"
        body_run = f"run_review{iteration}"
        events.extend(
            [
                StepBegin(
                    step=StepPath.parse(body_step),
                    kind="run",
                    given={
                        "statement": "run",
                        "runnable": "review",
                        "binding": "review",
                        "source": {"head": "let review = run review"},
                    },
                    placement={"iter": iteration, "iters": 3},
                ),
                RunBegin(
                    run=body_run,
                    parent=StepPath.parse(body_step),
                    control=ControlRef(body_run, 0),
                    runnable="agic:review",
                    placement={"iter": iteration},
                ),
                StepBegin(
                    step=StepPath.parse(f"{body_run}/0"),
                    kind="model",
                    given={"model": {"ref": "deepseek/deepseek-chat"}},
                ),
                StepEnd(
                    step=StepPath.parse(f"{body_run}/0"),
                    kind="model",
                    status="succeeded",
                    output=_parts(TextPart(f"review {iteration}")),
                ),
                RunEnd(
                    run=body_run,
                    status="succeeded",
                    output=_output(StepPath.parse(f"{body_run}/0")),
                ),
                StepEnd(
                    step=StepPath.parse(body_step),
                    kind="run",
                    status="succeeded",
                    output=_parts(TextPart(f"review {iteration}")),
                    noted={"shape": "item"},
                ),
            ]
        )
        until_run = f"run_until{iteration}"
        events.extend(
            [
                RunBegin(
                    run=until_run,
                    parent=StepPath.parse("run_one/2"),
                    control=ControlRef(until_run, 0),
                    runnable="agic:<agic:42>",
                    placement={"iter": -1},
                ),
                StepBegin(
                    step=StepPath.parse(f"{until_run}/0"),
                    kind="model",
                    given={"model": {"ref": "deepseek/deepseek-chat"}},
                ),
                StepEnd(
                    step=StepPath.parse(f"{until_run}/0"),
                    kind="model",
                    status="succeeded",
                    output=_parts(TextPart(decision)),
                ),
                RunEnd(
                    run=until_run,
                    status="succeeded",
                    output=_output(StepPath.parse(f"{until_run}/0")),
                ),
            ]
        )
    events.extend(
        [
            StepEnd(
                step=StepPath.parse("run_one/2"),
                kind="loop",
                status="succeeded",
            ),
            RunEnd(
                run="run_one",
                status="succeeded",
                output=_output(StepPath.parse("run_one/2/1")),
            ),
        ]
    )

    output = _render(events, verbosity=2)

    assert output.count("=== iteration 0 ===") == 1
    assert output.count("=== iteration 1 ===") == 1
    assert output.count("[0] let review = run review") == 2
    assert output.count("    Run agic review") == 2
    assert output.count("[?] until") == 2
    assert "    ↳ run_until0 succeeded" in output
    assert "    ↳ continue" in output
    assert "    ↳ stop repeating" in output
    assert "  ↳ stopped after 2 iterations" in output


def test_until_boolean_failure_has_no_control_decision() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step=StepPath.parse("run_one/0"),
                kind="loop",
                given={
                    "statement": "repeat",
                    "count": 2,
                    "binding": "_",
                    "source": {"head": "repeat 2"},
                },
            ),
            RunBegin(
                run="run_until",
                parent=StepPath.parse("run_one/0"),
                control=ControlRef("run_until", 0),
                runnable="agic:<agic:42>",
                placement={"iter": -1},
            ),
            StepBegin(
                step=StepPath.parse("run_until/0"),
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            StepEnd(
                step=StepPath.parse("run_until/0"),
                kind="model",
                status="succeeded",
                output=_parts(TextPart("yes")),
            ),
            RunEnd(
                run="run_until",
                status="succeeded",
                output=_output(StepPath.parse("run_until/0")),
            ),
            StepEnd(
                step=StepPath.parse("run_one/0"),
                kind="loop",
                status="failed",
                error="until requires a Boolean result",
            ),
            RunEnd(
                run="run_one",
                status="failed",
                error="until requires a Boolean result",
            ),
        ],
        verbosity=2,
    )

    assert "    ↳ run_until succeeded" in output
    assert "! run_one/0 failed: until requires a Boolean result" in output
    assert "↳ continue" not in output
    assert "↳ stop repeating" not in output


def test_failed_until_run_uses_a_red_compact_summary_without_a_decision() -> None:
    output = _render(
        [
            _flow_begin(),
            StepBegin(
                step=StepPath.parse("run_one/0"),
                kind="loop",
                given={
                    "statement": "repeat",
                    "count": 2,
                    "binding": "_",
                    "source": {"head": "repeat 2"},
                },
            ),
            RunBegin(
                run="run_until",
                parent=StepPath.parse("run_one/0"),
                control=ControlRef("run_until", 0),
                runnable="agic:<agic:42>",
                placement={"iter": -1},
            ),
            StepBegin(
                step=StepPath.parse("run_until/0"),
                kind="model",
                given={"model": {"ref": "deepseek/deepseek-chat"}},
            ),
            StepEnd(
                step=StepPath.parse("run_until/0"),
                kind="model",
                status="failed",
                error="provider returned status 429",
            ),
            RunEnd(
                run="run_until",
                status="failed",
                error="provider returned status 429",
            ),
            StepEnd(
                step=StepPath.parse("run_one/0"),
                kind="loop",
                status="failed",
                error="provider returned status 429",
            ),
            RunEnd(
                run="run_one",
                status="failed",
                error="provider returned status 429",
            ),
        ],
        tty=True,
    )

    assert output.count("provider returned status 429") == 1
    assert "\x1b[31m    ↳ run_until failed\x1b[0m" in output
    assert "↳ continue" not in output
    assert "↳ stop repeating" not in output


def _agic_begin(*, started_at: str = "") -> RunBegin:
    return RunBegin(
        run="run_one",
        control=ControlRef("run_one", 0),
        runnable="agic:demo",
        started_at=started_at,
    )


def _flow_begin(*, started_at: str = "") -> RunBegin:
    return RunBegin(
        run="run_one",
        control=ControlRef("run_one", 0),
        runnable="flow:research",
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
    parent: StepPath = StepPath.parse("run_one/2"),
) -> RunBegin:
    return RunBegin(
        run=run,
        parent=parent,
        control=ControlRef(run, 0),
        runnable=f"agic:{runnable}",
        placement={
            "item": item,
            "items": items,
            "lane": lane,
            "lanes": lanes,
        },
    )


def _sequential_run_begin(run: str, *, item: int) -> RunBegin:
    return RunBegin(
        run=run,
        parent=StepPath.parse("run_one/4"),
        control=ControlRef(run, 0),
        runnable="agic:reducer",
        placement={
            "item": item,
            "items": 2,
            "iter": item,
        },
    )
