"""Structural upserts and checkpoint atomicity independent of HTTP framing."""

from dataclasses import replace

import pytest

from toolang.execution.events import (
    RunBegin,
    RunEnd,
    RunRetried,
    RunSnapshot,
    StepBegin,
    StepEnd,
)
from toolang.execution.stream_client import StreamClientState
from toolang.execution.schemas import StreamFrame
from toolang.execution.types import ControlRef, EventCursor, StepRef
from toolang.lang.ast import RunStmt, Span


def cursor(seq):
    return str(EventCursor("a" * 32, seq))


def test_context_begin_never_clears_an_already_applied_end():
    state = StreamClientState()
    begin = RunBegin("run_one", ControlRef.for_run("run_one", 0))
    state.feed(StreamFrame.source(begin, cursor(1)))
    state.feed(StreamFrame.source(RunEnd("run_one", "succeeded"), cursor(2)))
    assert state.feed(StreamFrame.source(begin, cursor(1), context=True)) == ()
    assert state.complete("run_one")
    assert state.cursor == cursor(2)


def test_old_retry_control_cannot_clear_a_newer_incarnation():
    state = StreamClientState()
    begin = RunBegin("run_one", ControlRef.for_run("run_one", 2))
    state.feed(StreamFrame.source(begin, cursor(10)))
    state.feed(StreamFrame.source(RunEnd("run_one", "succeeded"), cursor(11)))
    mutation = RunRetried(
        "run_one", "term_one", ControlRef.for_run("run_one", 1), (), ()
    )
    assert state.feed(StreamFrame.source(mutation, cursor(12))) == ()
    assert state.complete("run_one")
    assert (
        state.feed(
            StreamFrame.source(
                replace(begin, control=ControlRef.for_run("run_one", 0)),
                cursor(1),
                context=True,
            )
        )
        == ()
    )
    assert state.complete("run_one")


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
    first = RunBegin("run_one", ControlRef.for_run("run_one", 0))
    second = RunBegin("run_two", ControlRef.for_run("run_two", 0))
    prefix = [
        (first, 110),
        (RunEnd("run_one", "succeeded"), 140),
        (second, 120),
        (RunEnd("run_two", "succeeded"), 130),
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
def test_nested_background_completion_waits_for_each_run_subtree(prefill):
    state = StreamClientState()
    root = RunBegin("run_root", ControlRef.for_run("run_root", 0))
    outer = StepBegin(
        StepRef.parse("run_root.0"),
        "run",
        RunStmt(span=Span(line=1), runnable="child", asynchronous=True),
    )
    child = RunBegin("run_child", ControlRef.for_run("run_child", 0), parent=outer.step)
    inner = StepBegin(
        StepRef.parse("run_child.0"),
        "run",
        RunStmt(span=Span(line=1), runnable="leaf", asynchronous=True),
    )
    leaf = RunBegin("run_leaf", ControlRef.for_run("run_leaf", 0), parent=inner.step)
    side = StepBegin(
        StepRef.parse("run_root.1"),
        "run",
        RunStmt(span=Span(line=1), runnable="sibling", asynchronous=True),
    )
    sibling = RunBegin(
        "run_sibling", ControlRef.for_run("run_sibling", 0), parent=side.step
    )
    child_end = RunEnd("run_child", "succeeded")
    events = (
        root,
        outer,
        child,
        StepEnd(outer.step, "run", "succeeded"),
        inner,
        leaf,
        StepEnd(inner.step, "run", "succeeded"),
        side,
        sibling,
        StepEnd(side.step, "run", "succeeded"),
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
        assert observations == ()
    assert child_end not in state.snapshot().events
    assert not state.complete(root.run)
    assert not state.complete(child.run)

    leaf_end = RunEnd(leaf.run, "succeeded")
    restored = state.feed(StreamFrame.source(leaf_end, cursor(seq + 1)))
    assert len(restored) == 1 and isinstance(restored[0], RunSnapshot)
    assert restored[0].events.index(leaf_end) < restored[0].events.index(child_end)
    assert state.complete(child.run)
    assert not state.complete(root.run)  # Its other branch is still running.
    root_end = RunEnd(root.run, "succeeded")
    assert state.feed(StreamFrame.source(root_end, cursor(seq + 2))) == ()
    finished = state.feed(
        StreamFrame.source(RunEnd(sibling.run, "succeeded"), cursor(seq + 3))
    )
    assert len(finished) == 1 and isinstance(finished[0], RunSnapshot)
    assert finished[0].events[-1] == root_end
    assert state.complete(root.run)
