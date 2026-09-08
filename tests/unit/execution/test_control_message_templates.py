"""Control templates preserve semantic input without execution identity."""

from __future__ import annotations

import pytest

from toolang.base.types.message import TextPart
from toolang.base.types.policy import RunLimits
from toolang.execution.control_messages import control_message
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
    SteerControlPayload,
)
from toolang.execution.types import (
    AgentResources,
    ControlRef,
    FieldRef,
    Local,
    MessageDelta,
    RunRef,
    ThreadRef,
    TypedRef,
)

from toolang.lang.input import CallInput


@pytest.mark.parametrize("reason", [None, "Please stop."])
def test_cancel_description_is_an_attribute(reason: str | None) -> None:
    control = ControlRecord(
        str(ControlRef.for_run("run_ab12", 1)),
        "cancel",
        CancelControlPayload(
            CallInput({"_": Local.typed("Text", reason).value})
            if reason is not None
            else CallInput({})
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
                AgentResources(),
                RunLimits(),
                None,
                "agent$agic:chat",
                "test/model",
                CallInput({}),
            ),
        ),
        ("retry", RetryControlPayload(AgentResources(), RunLimits(), None)),
        ("reload", ReloadControlPayload("a" * 64)),
        ("execute", ExecuteControlPayload("a" * 64, "agent$agic:chat", CallInput({}))),
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


@pytest.mark.parametrize("payload_type", [SteerControlPayload, CancelControlPayload])
def test_control_message_retains_referenced_input_type(payload_type) -> None:
    source = TypedRef(
        FieldRef.from_path(ControlRef.for_run("run_ab12", 0), "payload", "input", "_"),
        "Part[]",
    )
    control = ControlRecord(
        str(ControlRef.for_run("run_ab12", 1)),
        "steer" if payload_type is SteerControlPayload else "cancel",
        payload_type(CallInput({"_": source})),
    )
    template = control_message(control)
    assert template is not None
    assert template.segments[1] == TypedRef(
        FieldRef.from_path(control.ref, "payload", "input", "_"), "Part[]"
    )
