"""Control templates preserve semantic input without execution identity."""

from __future__ import annotations

import pytest

from toolang.base.types.message import Message, TextPart
from toolang.base.types.policy import RunLimits
from toolang.execution.control_messages import control_message
from toolang.execution.executor._messages import _MessageBuffer
from toolang.execution.message_delta import render_delta
from toolang.execution.records import (
    CancelControlPayload,
    ControlRecord,
    CreateControlPayload,
    ExecuteControlPayload,
    ForkControlPayload,
    ReloadControlPayload,
    RetryControlPayload,
    RewindControlPayload,
    RunControlPayload,
)
from toolang.execution.types import (
    AgentResources,
    ControlRef,
    Local,
    MessageDelta,
    RunRef,
    ThreadRef,
)


@pytest.mark.parametrize("reason", [None, "Please stop."])
def test_cancel_description_is_an_attribute(reason: str | None) -> None:
    control = ControlRecord(
        str(ControlRef.for_run("run_ab12", 1)),
        "cancel",
        CancelControlPayload(
            (Local.typed("Text", reason, "_"),) if reason is not None else (),
        ),
    )
    template = control_message(control)
    assert template is not None
    (message,) = render_delta(MessageDelta(messages=(template,)), lambda _: reason)
    opening = '<cancel description="The user canceled this run."'
    assert message.parts == (
        (TextPart(opening + ">"), TextPart(reason), TextPart("</cancel>"))
        if reason is not None
        else (TextPart(opening + "/>"),)
    )
    assert "run_ab12" not in str(message)


@pytest.mark.parametrize(
    "kind,payload",
    [
        (
            "run",
            RunControlPayload(
                AgentResources(), RunLimits(), None, "agent$agic:chat", "test/model", ()
            ),
        ),
        ("retry", RetryControlPayload(AgentResources(), RunLimits(), None)),
        ("reload", ReloadControlPayload("a" * 64)),
        ("execute", ExecuteControlPayload("a" * 64, "agent$agic:chat", ())),
        ("create", CreateControlPayload()),
        (
            "fork",
            ForkControlPayload(
                ThreadRef("term_ab12"),
                RunRef("run_ab12"),
                ControlRef.for_thread("term_ab12", 0),
            ),
        ),
        (
            "rewind",
            RewindControlPayload(
                RunRef("run_ab12"),
                RunRef("run_ab12"),
                ControlRef.for_thread("term_ab12", 0),
            ),
        ),
    ],
)
def test_other_controls_add_no_lifecycle_message(kind, payload) -> None:
    ref = (
        ControlRef.for_thread("term_ab12", 1)
        if kind in {"create", "fork", "rewind"}
        else ControlRef.for_run("run_ab12", 1)
    )
    assert control_message(ControlRecord(str(ref), kind, payload)) is None


def test_context_only_appends_when_changed_without_deduplicating_input() -> None:
    buffer = _MessageBuffer()
    context = "<context>one</context>"
    initial = [Message.user(context), Message.user("repeat")]
    buffer.initialize(initial, context=context)
    buffer.take_delta()
    staged = buffer.copy()
    staged.initialize(initial, context=context)
    assert staged.take_delta().messages == ()
    staged.append(Message.user("repeat"))
    staged.initialize(initial, context="<context>two</context>")
    assert staged.messages == [
        *initial,
        Message.user("repeat"),
        Message.user("<context>two</context>"),
    ]
    assert len(staged.take_delta().messages) == 2
    assert buffer.messages == initial
