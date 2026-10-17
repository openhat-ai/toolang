"""Structural upserts and checkpoint atomicity independent of HTTP framing."""

from toolang.execution.types import StepRef

from tests.support.observations import run_begin, run_end, step_begin, step_end

import pytest

from toolang.execution.events import (
    RunSnapshot,
)
from toolang.execution.stream_client import StreamClientState
from toolang.execution.schemas import StreamFrame
from toolang.execution.types import ControlRef, EventCursor
from toolang.lang.ast import RunStmt, Span


def cursor(seq):
    return str(EventCursor("a" * 32, seq))


def test_context_begin_never_clears_an_already_applied_end():
    state = StreamClientState()
    begin = run_begin("run_one", ControlRef.for_run("run_one", 0))
    state.feed(StreamFrame.source(begin, cursor(1)))
    state.feed(StreamFrame.source(run_end("run_one", "succeeded"), cursor(2)))
    assert state.feed(StreamFrame.source(begin, cursor(1), context=True)) == ()
    assert state.complete("run_one")
    assert state.cursor == cursor(2)


def test_grouped_prefix_commits_once_despite_out_of_order_original_cursors():
    state = StreamClientState()
    state.feed(StreamFrame.checkpoint(EventCursor.parse(cursor(100))))
    replacement = StreamFrame(
        "stream_prefill",
        {
            "cursor": cursor(150),
            "scope": {"kind": "agent"},
            "roots": ["run_one", "run_two"],
        },
    )
    state.feed(replacement)
    first = run_begin("run_one", ControlRef.for_run("run_one", 0))
    second = run_begin("run_two", ControlRef.for_run("run_two", 0))
    prefix = [
        (first, 110),
        (run_end("run_one", "succeeded"), 140),
        (second, 120),
        (run_end("run_two", "succeeded"), 130),
    ]
    for event, position in prefix[:2]:
        assert (
            state.feed(StreamFrame.source(event, cursor(position), context=True)) == ()
        )
    assert state.cursor == cursor(100)
    assert not state.complete("run_one")
    assert state.attach().events == ()
    state.feed(replacement)
    for event, position in prefix:
        state.feed(StreamFrame.source(event, cursor(position), context=True))
    result = state.feed(StreamFrame.checkpoint(EventCursor.parse(cursor(150))))
    assert len(result) == 1 and isinstance(result[0], RunSnapshot)
    assert state.cursor == cursor(150)
    assert state.complete("run_one") and state.complete("run_two")


def test_incomplete_prefill_rejects_mismatched_checkpoint():
    state = StreamClientState()
    state.feed(
        StreamFrame(
            "stream_prefill",
            {"cursor": cursor(2), "scope": {"kind": "agent"}, "roots": []},
        )
    )
    with pytest.raises(ValueError, match="does not complete"):
        state.feed(StreamFrame.checkpoint(EventCursor.parse(cursor(3))))
    assert state.cursor is None


@pytest.mark.parametrize("prefill", [False, True])
def test_parent_end_with_active_child_is_a_protocol_error(prefill):
    state = StreamClientState()
    events = (
        run_begin("run_root", 0),
        step_begin(
            "run_root.0",
            "run",
            RunStmt(span=Span(line=1), runnable="child", asynchronous=True),
        ),
        step_end("run_root.0", "run", "succeeded"),
        run_begin("run_child", 0, parent="run_root.0"),
        run_end("run_root", "succeeded"),
    )
    if prefill:
        state.feed(
            StreamFrame(
                "stream_prefill",
                {
                    "cursor": cursor(len(events)),
                    "scope": {"kind": "run", "id": "run_root"},
                    "roots": ["run_root"],
                },
            )
        )

    def consume():
        for seq, event in enumerate(events, 1):
            state.feed(StreamFrame.source(event, cursor(seq), context=prefill))
        if prefill:
            state.feed(StreamFrame.checkpoint(EventCursor.parse(cursor(len(events)))))

    with pytest.raises(ValueError, match="RunEnd with active child Run"):
        consume()


@pytest.mark.parametrize("prefill", [False, True])
def test_nested_background_completion_preserves_producer_order(prefill):
    state = StreamClientState()
    root = run_begin("run_root", ControlRef.for_run("run_root", 0))
    outer = step_begin(
        StepRef.parse("run_root.0"),
        "run",
        RunStmt(span=Span(line=1), runnable="child", asynchronous=True),
    )
    child = run_begin(
        "run_child", ControlRef.for_run("run_child", 0), parent=outer.step
    )
    inner = step_begin(
        StepRef.parse("run_child.0"),
        "run",
        RunStmt(span=Span(line=1), runnable="leaf", asynchronous=True),
    )
    leaf = run_begin("run_leaf", ControlRef.for_run("run_leaf", 0), parent=inner.step)
    side = step_begin(
        StepRef.parse("run_root.1"),
        "run",
        RunStmt(span=Span(line=1), runnable="sibling", asynchronous=True),
    )
    sibling = run_begin(
        "run_sibling", ControlRef.for_run("run_sibling", 0), parent=side.step
    )
    child_end = run_end("run_child", "succeeded")
    leaf_end = run_end(leaf.run, "succeeded")
    events = (
        root,
        outer,
        child,
        step_end(outer.step, "run", "succeeded"),
        inner,
        leaf,
        step_end(inner.step, "run", "succeeded"),
        side,
        sibling,
        step_end(side.step, "run", "succeeded"),
        leaf_end,
        child_end,
    )
    if prefill:
        state.feed(
            StreamFrame(
                "stream_prefill",
                {
                    "cursor": cursor(len(events)),
                    "scope": {"kind": "run", "id": root.run},
                    "roots": [root.run],
                },
            )
        )
    for seq, event in enumerate(events, 1):
        observations = state.feed(
            StreamFrame.source(event, cursor(seq), context=prefill)
        )
    if prefill:
        observations = state.feed(
            StreamFrame.checkpoint(EventCursor.parse(cursor(seq)))
        )
    else:
        assert observations == (child_end,)
    assert child_end in state.snapshot().events
    assert not state.complete(root.run)
    assert state.complete(child.run)
    sibling_end = run_end(sibling.run, "succeeded")
    assert state.feed(StreamFrame.source(sibling_end, cursor(seq + 1))) == (
        sibling_end,
    )
    root_end = run_end(root.run, "succeeded")
    assert state.feed(StreamFrame.source(root_end, cursor(seq + 2))) == (root_end,)
    assert state.complete(root.run)
