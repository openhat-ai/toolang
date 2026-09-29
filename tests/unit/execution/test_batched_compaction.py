"""Offline checks for the production full-exchange reducer."""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from toolang.execution import compaction as experiment
from toolang.base.errors import ModelResponseError
from toolang.base.types.message import Message, ToolCallPart, ToolResultPart
from toolang.base.types.model import Model
from toolang.base.types.run import ModelCallResult, ModelUsage
from toolang.execution.inspection.history import RunHistory
from toolang.execution.types import RunRef, ThreadRef


@pytest.fixture(autouse=True)
def offline_encoding(monkeypatch):
    # Exercise serialization, framing and model scales without downloading BPE data.
    class Encoding:
        def encode_ordinary(self, text):
            return list(text.encode("utf-8"))

    monkeypatch.setattr(experiment.tiktoken, "get_encoding", lambda name: Encoding())


class History:
    def __init__(self, values, statuses=None):
        self.values = values
        self.statuses = statuses or {}
        self.reads = []

    def thread_view(self, thread, *, include_children):
        assert thread == "term_test" and include_children is False
        return SimpleNamespace(
            roots=[
                SimpleNamespace(
                    id=ref,
                    status=self.statuses.get(ref, "succeeded"),
                )
                for ref in self.values
            ]
        )


class Adapter:
    def __init__(self, *, reject_combined=False):
        self.calls = []
        self.reject_combined = reject_combined

    async def invoke(self, model, request, *, environ):
        assert request.max_output_tokens is not None
        assert environ == {}
        self.calls.append(request)
        historical_text = "\n".join(
            part.text
            for message in request.messages
            for part in message.parts
            if hasattr(part, "text")
        )
        if (
            self.reject_combined
            and "run_a" in historical_text
            and "run_b" in historical_text
        ):
            self.reject_combined = False
            raise ModelResponseError(
                "model_context_window_exceeded", kind="provider_rejection"
            )
        return ModelCallResult(
            message=Message.assistant("updated summary"),
            usage=ModelUsage(input_tokens=42, output_tokens=5),
        )


def _unit_loader(history: History):
    def load(run_id: RunRef) -> experiment.HistoryUnit:
        history.reads.append(str(run_id))
        return experiment.HistoryUnit(
            run_id,
            history.statuses.get(str(run_id), "succeeded"),
            (Message.user(history.values[str(run_id)]),),
        )

    return load


def run_compact(
    history,
    adapter,
    from_="run_a",
    to="run_c",
    *,
    size=1024,
    max_output_tokens=None,
    model_ref="vercel/google/unknown",
    context=20000,
):
    async def execute():
        model = cast(
            Model,
            SimpleNamespace(ref=model_ref, limit={"context": context, "output": 4096}),
        )
        roots = experiment._select_runs(
            cast(RunHistory, history), ThreadRef("term_test"), RunRef(from_), RunRef(to)
        )
        reducer = experiment.Compaction(
            [RunRef(r.id) for r in roots],
            _unit_loader(history),
            model,
            size=size,
            max_output_tokens=max_output_tokens,
        )
        metrics = []
        while (call := reducer.next_call()) is not None:
            try:
                result = await adapter.invoke(model, call, environ={})
            except ModelResponseError as error:
                reducer.reject(error)
                continue
            metrics.append(
                {
                    "from": str(reducer.batch[0].run_id),
                    "to": str(reducer.batch[-1].run_id),
                    "usage_input_tokens": result.usage.input_tokens,
                }
            )
            reducer.accept(result)
        return reducer.summary, metrics

    return asyncio.run(execute())


def test_inclusive_range_and_lazy_full_exchange_read() -> None:
    history = History({"run_a": "first", "run_b": "second", "run_c": "third"})
    reader = experiment.HistoryReader(
        (RunRef("run_a"), RunRef("run_b")), _unit_loader(history)
    )
    assert history.reads == []
    assert reader.peek() == reader.peek()
    assert history.reads == ["run_a"]
    reader.advance()
    next_unit = reader.peek()
    assert next_unit is not None and next_unit.run_id == RunRef("run_b")
    reader.rewind(1)
    next_unit = reader.peek()
    assert next_unit is not None and next_unit.run_id == RunRef("run_a")
    assert experiment._select_runs(
        cast(RunHistory, history),
        ThreadRef.parse("term_test"),
        RunRef("run_b"),
        RunRef("run_c"),
    ) == tuple(
        SimpleNamespace(
            id=run_id,
            status="succeeded",
        )
        for run_id in ("run_b", "run_c")
    )
    with pytest.raises(ValueError, match="from must not"):
        experiment._select_runs(
            cast(RunHistory, history),
            ThreadRef.parse("term_test"),
            RunRef("run_c"),
            RunRef("run_a"),
        )


def test_failed_terminal_run_is_selectable_and_kept_in_history() -> None:
    history = History(
        {"run_a": "previous exchange", "run_b": "recorded error", "run_c": "later"},
        {"run_b": "failed"},
    )
    adapter = Adapter()
    summary, metrics = run_compact(history, adapter)
    assert summary == "updated summary"
    assert metrics[0]["from"] == "run_a"
    assert metrics[-1]["to"] == "run_c"
    assert "status=failed" in "\n".join(
        part.text
        for message in adapter.calls[0].messages
        for part in message.parts
        if hasattr(part, "text")
    )


def test_complete_tool_exchange_is_preserved_as_ordered_messages() -> None:
    call_id = "call-1"
    unit = experiment.HistoryUnit(
        RunRef("run_a"),
        "succeeded",
        (
            Message.user("question"),
            Message(
                "assistant",
                parts=(
                    ToolCallPart(
                        tool_call_id=call_id,
                        tool_name="lookup",
                        tool_family="test",
                        input={"query": "value"},
                    ),
                ),
            ),
            Message(
                "tool",
                parts=(
                    ToolResultPart(
                        tool_call_id=call_id,
                        tool_name="lookup",
                        tool_family="test",
                        output={"value": 42},
                    ),
                ),
            ),
            Message.assistant("answer"),
        ),
    )
    call = experiment._call("", (unit,), 128, 256)
    historical = call.messages[2:-2]
    assert [message.role for message in historical] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert isinstance(historical[1].parts[0], ToolCallPart)
    assert isinstance(historical[2].parts[0], ToolResultPart)


def test_context_overflow_retries_only_rejected_batch() -> None:
    history = History({"run_a": "first", "run_b": "second", "run_c": "third"})
    adapter = Adapter(reject_combined=True)
    summary, metrics = run_compact(history, adapter)
    assert summary == "updated summary"
    assert [(entry["from"], entry["to"]) for entry in metrics] == [
        ("run_a", "run_a"),
        ("run_b", "run_c"),
    ]
    assert all(entry["usage_input_tokens"] == 42 for entry in metrics)
    assert len(adapter.calls) == 3


def test_single_root_exchange_over_budget_does_not_invoke_model() -> None:
    history = History({"run_a": "x" * 150000, "run_b": "short"})
    adapter = Adapter()
    with pytest.raises(ValueError, match="one root Run exchange exceeds"):
        run_compact(history, adapter, to="run_b")
    assert not adapter.calls


def test_non_context_rejection_is_not_retried() -> None:
    class Rejecting(Adapter):
        async def invoke(self, model, request, *, environ):
            raise ModelResponseError("invalid request", kind="provider_rejection")

    history = History({"run_a": "first", "run_b": "second"})
    with pytest.raises(ModelResponseError, match="invalid request"):
        run_compact(history, Rejecting(), to="run_b")


def test_provider_usage_calibrates_future_admission() -> None:
    estimate = 500
    capacity = 1000
    assert experiment._estimate_fits(estimate, 1.0, capacity)
    scale = experiment._update_estimate_scale(
        1.0, estimate, ModelUsage(input_tokens=900, output_tokens=10)
    )
    assert scale == pytest.approx(1.8)
    assert not experiment._estimate_fits(estimate, scale, capacity)
    assert experiment._estimate_fits(800, 1.0, capacity)
    assert not experiment._estimate_fits(801, 1.0, capacity)
    # The original large root now clears the admission threshold on DeepSeek.
    assert experiment._estimate_fits(698233, 1.0, 941808)


def test_failed_run_usage_marker_uses_recorded_accounting() -> None:
    steps = (
        SimpleNamespace(
            kind="model",
            noted=SimpleNamespace(
                accounting=SimpleNamespace(input_tokens=1200, output_tokens=30)
            ),
        ),
        SimpleNamespace(kind="tool", noted=None),
    )
    assert experiment._run_usage(steps) == (1, 1, 1200, 30)


def test_summary_size_and_output_ceiling_are_independent() -> None:
    model = cast(Model, SimpleNamespace(limit={"context": 20000, "output": 4096}))
    default_output = experiment._output_allowance(model, 1024, None)
    explicit_output = experiment._output_allowance(model, 1024, 512)
    clamped_output = experiment._output_allowance(model, 4096, 100000)
    assert default_output == 2048
    assert explicit_output == 512
    assert clamped_output == 4096
    more_input = experiment.input_budget(model.limit, explicit_output)
    less_input = experiment.input_budget(model.limit, default_output)
    assert more_input is not None and less_input is not None
    assert more_input > less_input

    history = History({"run_a": "one complete exchange"})
    adapter = Adapter()
    run_compact(history, adapter, to="run_a", size=1024, max_output_tokens=512)
    assert adapter.calls[0].max_output_tokens == 512
    prompt_text = "\n".join(
        part.text
        for message in adapter.calls[0].messages
        for part in message.parts
        if hasattr(part, "text")
    )
    assert "approximately 1024 tokens" in prompt_text


def test_o200k_input_estimate_uses_exact_model_corrections() -> None:
    """Model correction remains distinct from admission and usage calibration."""
    import math

    import tiktoken

    call = experiment._call(
        "",
        (
            experiment.HistoryUnit(
                RunRef("run_a"),
                "succeeded",
                (Message.user("你好, a short exchange"),),
            ),
        ),
        512,
        4096,
    )
    encoding = tiktoken.get_encoding("o200k_base")
    fixed = {
        "instructions": call.instructions,
        "tools": [],
        "output_schema": None,
        "continuation": None,
        "reasoning": None,
    }
    import json

    base = 32 + len(encoding.encode_ordinary(json.dumps(fixed, ensure_ascii=False)))
    base += sum(
        8 + len(encoding.encode_ordinary(json.dumps(m.to_data(), ensure_ascii=False)))
        for m in call.messages
    )

    def model(ref: str) -> Model:
        return cast(Model, SimpleNamespace(ref=ref))

    assert (
        experiment.estimate_model_input_tokens(call, model("vercel/google/unknown"))
        == base
    )
    assert experiment.estimate_model_input_tokens(
        call, model("vercel/openai/gpt-6-luna-fast")
    ) == math.ceil(base * 0.92)
    assert experiment.estimate_model_input_tokens(
        call, model("vercel/anthropic/claude-sonnet-5")
    ) == math.ceil(base * 1.55)
    assert (
        experiment.estimate_model_input_tokens(call, model("deepseek/deepseek-flash"))
        == base
    )
    assert (
        experiment.estimate_model_input_tokens(
            call, model("vercel/anthropic/claude-opus-5.5")
        )
        == base
    )  # An unmeasured variant does not inherit Sonnet's correction.
    assert (
        experiment.estimate_model_input_tokens(
            experiment._call("", (), 512, 2048), model("deepseek/deepseek-flash")
        )
        > 0
    )
