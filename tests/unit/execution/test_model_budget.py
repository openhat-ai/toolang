"""Output allowance resolution, input admission, and context clipping."""

from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest

from toolang.base.types.message import Message, ImagePart
from toolang.base.types.model import Model, ModelToolang, Reasoning
from toolang.base.types.run import ModelCall
from toolang.base.types.tool import ToolDefinition
from toolang.execution.executor.budget import InputEstimate, message_tokens
from toolang.execution.executor.frame import _AgicFrame
from toolang.execution.executor.runs.agic import _AgicState
from toolang.execution.executor.steps.model import (
    _boundary,
    _clip_output,
    _estimate_binding,
)
from toolang.plugin.models.budget import (
    context_capacity,
    input_budget,
    output_budget,
)


MODEL = Model(
    id="model",
    name="model",
    _toolang=ModelToolang(provider="test", ready=True),
)


def test_input_budget_reserves_only_an_estimation_margin() -> None:
    model = replace(MODEL, limit={"context": 32000, "output": 8000})
    assert input_budget(model) == 30400
    assert input_budget(replace(model, limit={"input": 10000})) == 8976
    assert input_budget(MODEL) is None
    assert input_budget(replace(MODEL, limit={"input": 10000})) == 8976
    assert input_budget(replace(MODEL, limit={"context": 4000})) == 2976


def test_context_capacity_tracks_the_joint_window() -> None:
    assert context_capacity(MODEL) is None
    assert context_capacity(replace(MODEL, limit={"context": 32000})) == 32000


def test_output_allowance_defaults_to_the_model_maximum() -> None:
    assert output_budget(MODEL) is None
    assert output_budget(replace(MODEL, limit={"output": 100_000})) == 100_000


def test_explicit_max_output_is_clamped_by_the_model_limit() -> None:
    model = replace(MODEL, limit={"output": 8000})
    assert output_budget(model, demand=4096) == 4096
    assert output_budget(model, demand=99_000) == 8000
    assert output_budget(MODEL, demand=4096) == 4096


def test_output_allowance_must_exceed_an_explicit_reasoning_budget() -> None:
    model = replace(MODEL, limit={"output": 20_000})
    reasoning = Reasoning(budget_tokens=8000)

    with pytest.raises(ValueError, match="must exceed the reasoning budget"):
        output_budget(model, demand=4096, reasoning=reasoning)

    assert output_budget(model, demand=16_384, reasoning=reasoning) == 16_384
    assert output_budget(model, demand=None, reasoning=reasoning) == 20_000


def test_output_allowance_rejects_a_nonpositive_value() -> None:
    with pytest.raises(ValueError, match="max_output must be a positive integer"):
        output_budget(MODEL, demand=0)


def test_reliable_count_requires_calibration() -> None:
    request = ModelCall("instruct", [Message.user("input")])
    estimate = InputEstimate()

    assert estimate.reliable_count(request, "binding") is None

    estimate.observe(request, "binding", 900)

    assert estimate.reliable_count(request, "binding") == 900
    assert estimate.reliable_count(request, "other") is None


def test_clipping_trims_an_explicit_allowance_to_the_remaining_window() -> None:
    request = ModelCall("instruct", [Message.user("input")], max_output_tokens=50_000)
    frame = cast(
        _AgicFrame,
        SimpleNamespace(
            context_capacity=32_000,
            model=MODEL,
            run=SimpleNamespace(state=SimpleNamespace(revision="a"), horizon=None),
            recall=("near",),
        ),
    )
    estimate = InputEstimate()
    state = cast(_AgicState, SimpleNamespace(estimate=estimate))

    # Without a calibrated count the request is sent unchanged.
    assert _clip_output(state, frame, request).max_output_tokens == 50_000

    estimate.observe(request, _estimate_binding(frame), 31_000)

    assert _clip_output(state, frame, request).max_output_tokens == 1000


def test_estimate_calibrates_only_an_unchanged_prefix() -> None:
    estimate = InputEstimate()
    request = ModelCall(
        "instruct",
        [Message.user("first")],
        (ToolDefinition("t", "guidance"),),
        {"type": "string"},
    )
    estimate.observe(request, "binding", 800)
    appended = Message.assistant("response")
    next_call = replace(request, messages=[*request.messages, appended])
    assert estimate.count(next_call, "binding") == 800 + message_tokens(appended)
    for changed in (
        replace(next_call, instructions="new"),
        replace(next_call, tools=()),
        replace(next_call, output_schema=None),
        replace(next_call, messages=[appended]),
    ):
        assert estimate.count(changed, "binding") == InputEstimate().count(
            changed, "binding"
        )
    assert estimate.count(next_call, "new horizon") == InputEstimate().count(
        next_call, "new horizon"
    )
    assert (
        message_tokens(
            Message("user", (ImagePart(image_url="https://example.invalid/image"),))
        )
        >= 4096
    )


def test_exact_budget_fits_but_one_more_token_requires_action() -> None:
    request = ModelCall("instruct", [Message.user("input")])
    count = InputEstimate().count(request, None)
    state = cast(_AgicState, SimpleNamespace(estimate=InputEstimate(), execution=None))
    frame = SimpleNamespace(
        input_budget=count,
        model=MODEL,
        run=SimpleNamespace(state=SimpleNamespace(revision="a"), horizon=None),
        recall=("near",),
    )
    assert _boundary(state, cast(_AgicFrame, frame), request) is None
    frame.input_budget -= 1
    with pytest.raises(Exception, match="input exceeds"):
        _boundary(state, cast(_AgicFrame, frame), request)


def test_missing_provider_usage_reuses_estimated_prefix(monkeypatch) -> None:
    from toolang.execution.executor import budget

    request = ModelCall("instruct", [Message.user("first")])
    estimate = InputEstimate()
    expected = estimate.count(request, "a")
    estimate.observe(request, "a", None)
    monkeypatch.setattr(
        budget,
        "message_tokens",
        lambda _message: pytest.fail("stable prefix was reestimated"),
    )
    assert estimate.count(request, "a") == expected
