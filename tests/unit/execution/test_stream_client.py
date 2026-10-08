"""Structural upserts and checkpoint atomicity independent of HTTP framing."""

from dataclasses import replace

import pytest

from toolang.execution.events import RunBegin, RunEnd, RunRetried, RunSnapshot
from toolang.execution.stream_client import StreamClientState
from toolang.execution.schemas import StreamFrame
from toolang.execution.types import ControlRef, EventCursor


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
