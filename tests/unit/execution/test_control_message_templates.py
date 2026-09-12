"""Control templates preserve semantic input without execution identity."""

from __future__ import annotations

import pytest

from toolang.base.types.message import TextPart
from toolang.base.types.policy import RunLimits
from toolang.execution.assembly.messages import control_message
from toolang.execution.assembly.utils import render_delta
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
    MessageDelta,
    RunRef,
    ThreadRef,
    TypedRef,
)
from toolang.lang.input import CallInput


@pytest.mark.parametrize("reason", [None, "Please stop."])
@pytest.mark.parametrize(
    "kind,payload_type,description",
    [
        ("cancel", CancelControlPayload, "The user canceled this run."),
        (
            "steer",
            SteerControlPayload,
            "The user supplied updated input for the current task.",
        ),
    ],
)
def test_control_description_is_an_attribute(
    reason: str | None, kind, payload_type, description
) -> None:
    control = ControlRecord(
        str(ControlRef.for_run("run_ab12", 1)),
        kind,
        payload_type(CallInput({"_": reason}) if reason is not None else CallInput({})),
    )
    template = control_message(control)
    assert template is not None
    (message,) = render_delta(MessageDelta(messages=(template,)), lambda _: reason)
    opening = f'<toolang:{kind} description="{description}"'
    assert message.parts == (
        (TextPart(opening + ">"), TextPart(reason), TextPart(f"</toolang:{kind}>"))
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
