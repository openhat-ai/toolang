"""The model record contract retains call facts without duplicate projections."""

from dataclasses import fields, replace
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import TypeAdapter, ValidationError

from toolang.base.types.message import Message
from toolang.base.types.model import Model, ModelRequest, ModelToolang, Reasoning
from toolang.base.types.run import ModelCall, ModelUsage
from toolang.base.types.tool import ToolDefinition
from toolang.execution.accounting import (
    build_model_accounting,
    selected_cost_is_approximate,
    selected_usd_cost,
)
from toolang.execution.records import (
    StoredModelStepGiven,
    step_noted_from_data,
    step_noted_to_data,
)
from toolang.execution.store import RunStore
from toolang.execution.types import (
    ControlRef,
    ModelCost,
    ModelCostLine,
    ModelPricing,
    ModelStepGiven,
    ModelStepNoted,
    ModelUsageMeter,
    StepRef,
)


def test_call_settings_and_setup_survive_store_reopening(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    ref = StepRef.parse("run_contract.0")
    call = ModelCall(
        instructions="Be concise.",
        messages=[Message.user("hello")],
        tools=(ToolDefinition(name="test", description="A test tool"),),
        output_schema={"type": "string"},
        continuation={"cursor": "before"},
        max_output_tokens=512,
        reasoning=Reasoning(budget_tokens=128),
    )
    with closing(RunStore(path)) as store:
        store.begin_step(
            ref=ref,
            kind="model",
            input=(),
            state=ControlRef.for_run(ref.run_id, 0),
            started_at="now",
            given=ModelStepGiven("test/one", call, setup="setup-v1"),
        )
    with closing(RunStore(path)) as store:
        step = store.get_step(ref=ref)
        assert step is not None and isinstance(step.given, StoredModelStepGiven)
        assert step.given.setup == "setup-v1"
        assert step.given.call.reasoning == call.reasoning
        assert store.rebuild_model_call(step) == call
        assert {"tokens", "price", "cost"}.isdisjoint(
            item.name for item in fields(ModelStepNoted)
        )
        assert {item.name for item in fields(ModelPricing)} == {"plan", "match"}


@pytest.mark.parametrize(
    ("rates", "usage", "selected", "amount", "complete"),
    [
        ({"input": 0, "output": 0}, ModelUsage(2, 2), "zero", 0.0, True),
        (None, ModelUsage(2, 2), "unknown", None, None),
        ({"input": 0.1, "output": 0.1}, ModelUsage(1, 1), "estimated", 0.0, True),
        ({"input": 0}, ModelUsage(2, 2), "estimated", 0.0, False),
        (
            None,
            ModelUsage(2, 2, reported_cost=0.0, reported_currency="USD"),
            "reported",
            0.0,
            True,
        ),
        (
            {"input": 1, "output": 2},
            ModelUsage(2, 2, reported_cost=0.5, reported_currency="USD"),
            "reported",
            0.5,
            True,
        ),
        (
            None,
            ModelUsage(2, 2, reported_cost=0.5, reported_currency="EUR"),
            "reported",
            None,
            True,
        ),
    ],
)
def test_cost_selection_survives_numeric_record_round_trip(
    rates: dict[str, object] | None,
    usage: ModelUsage,
    selected: str,
    amount: float | None,
    complete: bool | None,
) -> None:
    model = Model("one", "One", ModelToolang(provider="test"), cost=rates)
    accounting = build_model_accounting(model, usage)
    assert accounting is not None and accounting.selected == selected
    noted = ModelStepNoted(accounting=accounting, continuation={"cursor": "after"})
    data = step_noted_to_data("model", noted)
    assert data is not None and set(data) == {"accounting", "cont"}
    restored = step_noted_from_data("model", data)
    assert restored == noted
    assert isinstance(restored, ModelStepNoted) and restored.accounting is not None
    assert selected_usd_cost(restored.accounting) == amount
    result = restored.accounting.reported or restored.accounting.estimate
    assert (result.complete if result else None) == complete
    assert selected_cost_is_approximate(restored.accounting) == (
        selected in {"estimated", "unknown"}
    )
    wire = cast(dict[str, Any], data["accounting"])
    assert "reasoning" not in wire
    if wire["pricing"] is not None:
        assert set(wire["pricing"]) == {"plan", "match"}
    for cost in (wire["cost"]["reported"], wire["cost"]["estimate"]):
        if cost is not None:
            assert isinstance(cost["amount"], float)


@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan"), "1.2"])
def test_accounting_rejects_invalid_numeric_values(value: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        ModelUsageMeter("input", value)
    with pytest.raises((TypeError, ValueError)):
        ModelCost(value, "USD", True)
    line = ModelCostLine("input", 1, "token", 1, 1_000_000, 0.000001)
    for name in ("quantity", "rate", "per", "amount"):
        with pytest.raises((TypeError, ValueError)):
            replace(line, **{name: value})


def test_cost_denominator_cannot_be_zero() -> None:
    with pytest.raises(ValueError, match="positive"):
        ModelCostLine("input", 1, "token", 1, 0, 0)


def test_flat_model_request_rejects_the_removed_parameters_wrapper() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ModelRequest).validate_python({"ref": "test/one", "parameters": {}})
