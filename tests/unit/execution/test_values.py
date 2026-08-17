from __future__ import annotations

from typing import Literal

import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import TextPart, ToolCallPart
from toolang.base.types.policy import RunLimits
from toolang.execution.records import (
    RunControlRecord,
    RetryControlPayload,
    StartControlPayload,
    SteerControlPayload,
    StopControlPayload,
    control_payload_from_data,
    control_payload_to_data,
    local_from_data,
    local_to_data,
)
from toolang.execution.schemas import ControlInfo
from toolang.execution.types import AgentResources, Local, StepPath, ValuePtr


def test_value_pointer_accepts_run_step_control_and_json_paths() -> None:
    assert str(ValuePtr("run_1")) == "run_1"
    assert ValuePtr("run_1.0.2/key~1name/1").anchor == "run_1.0.2"
    assert ValuePtr("run_1.0.2/key~1name/1").pointer == "key~1name/1"
    assert ValuePtr("run_1^3/_/1").anchor == "run_1^3"


@pytest.mark.parametrize(
    "value",
    (
        "",
        "run.01",
        "run.0/key/",
        "run.0/key~2name",
        "run^0",
        "run^x/_",
        "run@file",
    ),
)
def test_value_pointer_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError):
        ValuePtr(value)


def test_value_pointer_accepts_a_whole_value_slash() -> None:
    assert ValuePtr("run/").pointer == ""


def test_local_keeps_complete_type_separate_from_execution_dimension() -> None:
    response = Local(
        type="Part[]",
        value=(),
        name="_",
        dim=0,
    )
    scattered = Local(
        type="Part[]",
        value=ValuePtr("run_1.0"),
        name="_",
        dim=1,
    )
    batches = Local(
        type="Part[][]",
        value=(ValuePtr("run_a"), ValuePtr("run_b")),
        name="_",
        dim=1,
    )

    assert response.item_type == "Part[]"
    assert scattered.item_type == "Part"
    assert batches.item_type == "Part[]"


def test_dim_one_requires_an_array_type_and_value() -> None:
    with pytest.raises(ValueError, match="array value type"):
        Local(type="Text", value=("one",), dim=1)
    with pytest.raises(TypeError, match="array value"):
        Local(type="Text[]", value="one", dim=1)


def test_local_codec_round_trips_mixed_concrete_and_pointer_items() -> None:
    local = Local(
        type="Part[]",
        value=(
            TextPart("kept"),
            ValuePtr("run_1.0/2"),
            ToolCallPart(
                tool_call_id="call_1",
                tool_name="search",
                tool_family="search",
                input={"query": "toolang"},
            ),
        ),
        name="_",
        dim=1,
    )

    assert local_from_data(local_to_data(local)) == local


def test_local_codec_reserves_the_pointer_marker() -> None:
    local = Local(type="Json", value={"$ptr": "ordinary data"})

    with pytest.raises(ValueError, match="reserved"):
        local_to_data(local)


def test_preparation_payload_round_trips_resolved_locals() -> None:
    payload = StartControlPayload(
        resources=AgentResources(models=("test/model",)),
        limits=RunLimits(tokens=10),
        runnable="agic:worker",
        model="test/model",
        locals=(Local("Part[]", (TextPart("hello"),), "_", 0),),
    )

    assert (
        control_payload_from_data("start", control_payload_to_data(payload)) == payload
    )


def test_retry_payload_distinguishes_inherited_and_empty_locals() -> None:
    inherited = RetryControlPayload(
        resources=AgentResources(models=("test/model",)),
        limits=RunLimits(),
        runnable="flow:worker",
        model="test/model",
        locals=None,
        retry_from=StepPath("run_1", (2,)),
    )
    empty = RetryControlPayload(
        resources=inherited.resources,
        limits=inherited.limits,
        runnable=inherited.runnable,
        model=inherited.model,
        locals=(),
        retry_from=None,
    )

    assert (
        control_payload_from_data("retry", control_payload_to_data(inherited))
        == inherited
    )
    assert control_payload_from_data("retry", control_payload_to_data(empty)) == empty


@pytest.mark.parametrize(
    ("kind", "payload"),
    (
        (
            "steer",
            SteerControlPayload((Local("Part[]", (TextPart("continue"),), "_", 0),)),
        ),
        ("stop", StopControlPayload()),
    ),
)
def test_control_protocol_uses_kind_to_restore_payload_variant(
    kind: Literal["steer", "stop"],
    payload: SteerControlPayload | StopControlPayload,
) -> None:
    record = RunControlRecord(
        target="run_test",
        index=1,
        kind=kind,
        payload=payload,
    )
    info = ControlInfo(
        run_id="run_test",
        index=record.index,
        kind=kind,
        timing=record.timing,
        request_id=None,
        status=record.status,
        payload=payload,
        error=None,
        created_at="",
        finished_at=None,
    )

    restored_record = TypeAdapter(RunControlRecord).validate_python(
        TypeAdapter(RunControlRecord).dump_python(record, mode="json")
    )
    restored_info = TypeAdapter(ControlInfo).validate_python(
        TypeAdapter(ControlInfo).dump_python(info, mode="json")
    )

    assert type(restored_record.payload) is type(payload)
    assert type(restored_info.payload) is type(payload)
