"""Input reservation and calibration use the exact normalized request."""

from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest

from toolang.base.types.message import Message, ImagePart
from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.base.types.run import ModelCall
from toolang.base.types.tool import ToolDefinition
from toolang.execution.executor.budget import InputEstimate, message_tokens
from toolang.execution.executor.frame import _AgicFrame
from toolang.execution.executor.runs.agic import _AgicState
from toolang.execution.executor.steps.model import _boundary
from toolang.plugin.models.budget import input_budget, output_budget


INFO = ModelInfo(ref="test/model", provider="test", name="model", model="model")
TARGET = ModelTarget(
    ref="test/model", provider="test", name="model", model="model", adapter="responses"
)


def test_input_budget_reserves_output_and_independent_limit() -> None:
    info = replace(INFO, context_window=32000, max_output_tokens=8000)
    assert output_budget(TARGET, info) == 4096
    assert input_budget(info, 4096) == 26508
    assert (
        input_budget(replace(info, metadata={"limit": {"input": 10000}}), 4096) == 8976
    )
    assert input_budget(INFO, 4096) is None
    assert (
        input_budget(replace(INFO, metadata={"limit": {"input": 10000}}), 4096) == 8976
    )
    assert input_budget(replace(INFO, context_window=4000), 4096) == 0


@pytest.mark.parametrize(
    "adapter, options",
    [
        ("responses", {"max_output_tokens": 1200}),
        ("chat_completions", {"max_completion_tokens": 1200}),
        ("chat_completions", {"max_tokens": 1200}),
        ("messages", {"max_tokens": 1200}),
        ("generate_content", {"generationConfig": {"maxOutputTokens": 1200}}),
    ],
)
def test_native_configuration_and_model_output_limit(adapter, options) -> None:
    target = replace(TARGET, adapter=adapter, options=options)
    assert output_budget(target, INFO) == 1200
    assert output_budget(target, replace(INFO, max_output_tokens=1000)) == 1000


def test_reasoning_is_included_once_in_output_budget() -> None:
    target = replace(TARGET, adapter="messages", reasoning={"budget_tokens": 5000})
    assert output_budget(target, INFO) == 5001
    assert output_budget(replace(target, options={"max_tokens": 6000}), INFO) == 6000
    with pytest.raises(ValueError, match="thinking"):
        output_budget(target, replace(INFO, max_output_tokens=4000))


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
        model=TARGET,
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
