"""Output allowance resolution, input admission, and context clipping."""

from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest

from toolang.base.types.message import Message, ImagePart
from toolang.base.types.model import Model, ModelRoute, ModelToolang, Reasoning
from toolang.base.types.run import ModelCall
from toolang.base.types.tool import ToolDefinition
from toolang.execution.executor.budget import InputEstimate, message_tokens
from toolang.execution.executor.frame import _AgicFrame
from toolang.execution.executor.runs.agic import _AgicState
from toolang.execution.executor.steps.model import (
    _boundary,
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


def test_input_budget_reserves_output_and_an_estimation_margin() -> None:
    info = replace(MODEL, limit={"context": 32000, "output": 8000})
    assert input_budget(info, 8000) == 22400
    assert (
        input_budget(replace(info, limit={"context": 32000, "input": 10000}), 8000)
        == 8976
    )
    assert input_budget(MODEL, None) is None
    assert input_budget(replace(MODEL, limit={"input": 10000}), None) == 8976
    assert input_budget(replace(MODEL, limit={"context": 4000}), 1000) == 1976


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


@pytest.mark.parametrize("calibrated", [False, True])
def test_reported_context_overflow_is_rejected_before_dispatch(calibrated) -> None:
    info = replace(MODEL, limit={"context": 1048576, "output": 384000})
    request = ModelCall(
        "", [Message.user("x" * (665128 * 3))], max_output_tokens=384000
    )
    frame = cast(
        _AgicFrame,
        SimpleNamespace(
            input_budget=input_budget(info, output_budget(info)),
            input_overhead=0,
            model=MODEL,
            reasoning=None,
            run=SimpleNamespace(state=SimpleNamespace(revision="a"), horizon=None),
            recall=("near",),
        ),
    )
    estimate = InputEstimate()
    if calibrated:
        estimate.observe(request, _estimate_binding(frame), 665128)
    state = cast(_AgicState, SimpleNamespace(estimate=estimate, execution=None))
    with pytest.raises(Exception, match="input exceeds"):
        _boundary(state, frame, request)
    assert request.max_output_tokens == 384000


@pytest.mark.parametrize("output", [None, 32000])
def test_known_context_requires_a_resolvable_output_and_positive_input_room(output):
    with pytest.raises(ValueError):
        input_budget(replace(MODEL, limit={"context": 32000}), output)
    with pytest.raises(ValueError, match="known context"):
        output_budget(replace(MODEL, limit={"context": 32000}))


@pytest.mark.parametrize(
    ("adapter", "options"),
    [
        ("responses", {"max_output_tokens": 1234}),
        ("messages", {"max_tokens": 1234}),
        ("chat_completions", {"max_completion_tokens": 1234}),
        ("chat_completions", {"max_tokens": 1234}),
        ("generate_content", {"generationConfig": {"maxOutputTokens": 1234}}),
    ],
)
def test_provider_output_fallback_is_resolved_before_admission(adapter, options):
    model = replace(MODEL, limit={"context": 32000}).with_route(
        ModelRoute(adapter=adapter, options=options)
    )
    assert output_budget(model) == 1234
    assert output_budget(model, demand=1000) == 1000


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
        replace(next_call, continuation={"reasoning": {"id": "text"}}),
        replace(next_call, reasoning=Reasoning(effort="high")),
        replace(next_call, instructions="new"),
        replace(next_call, tools=()),
        replace(next_call, output_schema=None),
        replace(next_call, messages=[appended]),
    ):
        assert estimate.reliable_count(changed, "binding") is None
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
        input_overhead=0,
        model=MODEL,
        reasoning=None,
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
    assert estimate.reliable_count(request, "a") is None


def test_adapter_overhead_participates_in_admission_and_calibration():
    request = ModelCall("instructions", [Message.user("hello")])
    estimate = InputEstimate()
    base = estimate.count(request, "a")
    assert estimate.count(request, "a", 3000) == base + 3000
    estimate.observe(request, "a", 3500, 3000)
    assert estimate.count(request, "a", 3000) == 3500
    assert estimate.reliable_count(request, "a", 3000) == 3500
    assert estimate.reliable_count(request, "a", 4000) is None
    assert estimate.count(request, "a", 4000) == base + 4000


def test_schema_directive_and_continuation_are_counted():
    request = ModelCall("instructions", [Message.user("hello")])
    enriched = replace(
        request,
        output_schema={"type": "string"},
        continuation={"reasoning": {"id": "thinking " * 1000}},
    )
    estimate = InputEstimate()
    assert estimate.count(enriched, None) > estimate.count(request, None) + 3000
