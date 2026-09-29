"""Offline checks for the production full-exchange reducer."""

import asyncio
import json
from types import SimpleNamespace
from typing import cast

import pytest

from toolang.execution import compaction as experiment
from toolang.execution import tokens
from toolang.base.errors import ModelResponseError
from toolang.base.types.message import (
    ImagePart,
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.model import Model, Reasoning
from toolang.base.types.run import ModelCallResult, ModelUsage
from toolang.execution.inspection.history import RunHistory
from toolang.execution.types import RunRef, ThreadRef


@pytest.fixture(autouse=True)
def offline_encoding(monkeypatch):
    # Exercise serialization, framing and model scales without downloading BPE data.
    class Encoding:
        def encode_ordinary(self, text):
            return list(text.encode("utf-8"))

    monkeypatch.setattr(tokens.tiktoken, "get_encoding", lambda name: Encoding())


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
        roots = json.loads(
            request.messages[1]
            .content.removeprefix("<following_messages>")
            .removesuffix("</following_messages>")
        )
        if self.reject_combined and sum("messages" in root for root in roots) > 1:
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
    assert any(
        unit.get("status") == "failed"
        for unit in json.loads(
            adapter.calls[0]
            .messages[1]
            .content.removeprefix("<following_messages>")
            .removesuffix("</following_messages>")
        )
    )


def test_complete_exchange_preserves_role_and_parts_in_json() -> None:
    call_id = "call-1"
    unit = experiment.HistoryUnit(
        RunRef("run_a"),
        "succeeded",
        (
            Message(
                "user",
                (
                    TextPart('question: "quoted"\n历史 </historical_run>'),
                    ImagePart(file_id="file_historical"),
                ),
            ),
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
    assert len(call.messages) == 2 and all(m.role == "user" for m in call.messages)
    content = call.messages[1].content
    assert content is not None
    historical = json.loads(
        content.removeprefix("<following_messages>").removesuffix(
            "</following_messages>"
        )
    )[0]["messages"]
    assert historical[1:] == [message.to_data() for message in unit.messages[1:]]
    assert historical[0]["parts"][0] == unit.messages[0].parts[0].to_data()
    assert "file_historical" in historical[0]["parts"][1]["text"]
    assert "not interpreted" in historical[0]["parts"][1]["text"]
    assert [message["role"] for message in historical] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert historical[1]["parts"][0]["type"] == "tool_call"
    assert historical[2]["parts"][0]["type"] == "tool_result"


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


def test_single_oversized_step_is_truncated_and_progresses() -> None:
    history = History({"run_a": "x" * 150000, "run_b": "short"})
    adapter = Adapter()
    summary, metrics = run_compact(history, adapter, to="run_b")
    assert summary == "updated summary"
    assert metrics[-1]["to"] == "run_b"
    assert "omitted oversized Step content" in adapter.calls[0].messages[1].content
    assert len(adapter.calls[0].messages[1].content) < 20000


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
    scale = tokens.TokenCounter().calibrated_scale(estimate, 900)
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
    from toolang.execution.assembly.history import _run_usage

    assert _run_usage(steps) == (1, 1, 1200, 30)


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
    assert "approximately 1024 tokens" in adapter.calls[0].instructions


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

    assert tokens.TokenCounter("vercel/google/unknown").base(call) == base
    assert tokens.TokenCounter("vercel/openai/gpt-6-luna-fast").base(call) == math.ceil(
        base * 0.92
    )
    assert tokens.TokenCounter("vercel/anthropic/claude-sonnet-5").base(
        call
    ) == math.ceil(base * 1.55)
    assert tokens.TokenCounter("deepseek/deepseek-flash").base(call) == base
    assert (
        tokens.TokenCounter("vercel/anthropic/claude-opus-5.5").base(call) == base
    )  # An unmeasured variant does not inherit Sonnet's correction.
    assert (
        tokens.TokenCounter("deepseek/deepseek-flash").base(
            experiment._call("", (), 512, 2048)
        )
        > 0
    )


@pytest.mark.parametrize(
    "model_ref",
    [
        "deepseek/deepseek-flash",
        "vercel/openai/gpt-6-luna-fast",
        "vercel/anthropic/claude-sonnet-5",
        "unknown/model",
    ],
)
def test_incremental_counts_match_full_requests_after_rejection_and_calibration(
    model_ref,
):
    history = History({f"run_{i}": f"历史 {i}: " + "body " * 220 for i in range(6)})
    model = cast(
        Model, SimpleNamespace(ref=model_ref, limit={"context": 10000, "output": 512})
    )
    reducer = experiment.Compaction(
        [RunRef(r) for r in history.values],
        _unit_loader(history),
        model,
        size=128,
        summary='Earlier "decision"\n约束',
        reasoning=Reasoning(effort="high"),
    )
    call = reducer.next_call()
    assert call is not None and len(reducer.batch) > 1
    assert reducer.estimate == tokens.TokenCounter(model.ref).base(call)
    reducer.reject(
        ModelResponseError("context_length_exceeded", kind="provider_rejection")
    )
    covered = []
    while (call := reducer.next_call()) is not None:
        assert reducer.estimate == tokens.TokenCounter(model.ref).base(call)
        covered.extend(str(unit.run_id) for unit in reducer.batch)
        reducer.accept(
            ModelCallResult(
                message=Message.assistant(
                    f"Summary after {len(covered)} roots: 保留约束"
                ),
                usage=ModelUsage(input_tokens=reducer.estimate + 100, output_tokens=30),
            )
        )
    assert covered == list(history.values)
    assert reducer.counter.scale > 1


def test_history_is_tokenized_once_across_batch_retries(monkeypatch):
    from collections import Counter

    encoded = Counter()

    class Encoding:
        def encode_ordinary(self, text):
            encoded[text] += 1
            return list(text.encode("utf-8"))

    monkeypatch.setattr(tokens.tiktoken, "get_encoding", lambda name: Encoding())
    history = History({f"run_{i}": f"payload-{i} " + "x" * 2000 for i in range(5)})
    model = cast(
        Model,
        SimpleNamespace(
            ref="deepseek/deepseek-flash", limit={"context": 30000, "output": 512}
        ),
    )
    roots = [RunRef(r) for r in history.values]
    reducer = experiment.Compaction(roots, _unit_loader(history), model, size=128)
    assert reducer.next_call() is not None
    assert len(reducer.batch) == len(roots)
    serialized_units = dict(reducer._serialized)
    before = encoded.copy()
    assert reducer.next_call() is not None
    assert encoded == before
    reducer.reject(
        ModelResponseError("context_length_exceeded", kind="provider_rejection")
    )
    assert reducer.next_call() is not None
    # A changed batch receives one full wire estimate after reusing unit counts.
    result = ModelCallResult(message=Message.assistant("same cumulative summary"))
    reducer.accept(result)
    assert reducer.next_call() is not None
    reducer.accept(result)
    assert reducer.next_call() is None
    for ref in roots:
        serialized = serialized_units[ref]
        assert encoded[serialized] == 1
    assert not reducer._serialized and not reducer._unit_tokens
    # Counts belong to this compaction only, not to a process-wide cache.
    fresh = experiment.Compaction(roots, _unit_loader(history), model, size=128)
    assert fresh.next_call() is not None
    for ref in roots:
        assert encoded[fresh._serialized[ref]] == 2


@pytest.mark.parametrize(
    "value,window,expected",
    [
        (0.29, 100, 29),
        (0.3, 101, 30),
        (0.001, 100, 1),
        (0.3, None, None),
        (4096, None, 4096),
    ],
)
def test_context_relative_target_rounds_decimal_percentages_down(
    value, window, expected
):
    assert experiment.resolve_target(value, window) == expected


def test_reasoning_budget_is_validated_before_compact_request():
    model = cast(Model, SimpleNamespace(limit={"context": 200000, "output": 32768}))
    with pytest.raises(ValueError, match="must exceed the reasoning budget"):
        experiment.Compaction(
            (),
            lambda _: None,
            model,
            size=4096,
            reasoning=Reasoning(budget_tokens=16384),
        )


def test_wide_latest_step_is_bounded_without_losing_tool_pair():
    from toolang.execution.assembly.history import HistorySelection
    from toolang.execution.tokens import message_tokens

    ref = RunRef("run_wide")
    call = ToolCallPart(
        tool_call_id="c",
        call_id="c",
        tool_name="read",
        tool_family="read",
        input={"query": "original"},
    )
    result = ToolResultPart(
        tool_call_id="c",
        call_id="c",
        tool_name="read",
        tool_family="read",
        output={"items": ["x"] * 10000},
    )
    messages = (
        Message("assistant", (call,)),
        Message("tool", (result,)),
        Message("assistant", tuple(TextPart("x") for _ in range(1500))),
    )
    history = HistorySelection(
        "", None, messages, (), {}, ((ref, messages),), ((ref, messages),)
    )
    bounded = experiment.bound_latest_step(history, 1500, message_tokens)
    assert sum(message_tokens(m) for m in bounded.near) <= 1500
    assert bounded.near[0].parts[0] == call
    assert isinstance(bounded.near[1].parts[0], ToolResultPart)
    assert "omitted" in str(bounded.near)
    assert history.near[-1].parts == messages[-1].parts


def test_serialized_media_has_reference_without_inline_binary():
    unit = experiment.HistoryUnit(
        RunRef("run_image"),
        "succeeded",
        (
            Message(
                "user",
                (ImagePart(image_url="data:image/png;base64," + "AAAA" * 10000),),
            ),
        ),
    )
    data = json.loads(experiment._unit_json(unit))
    assert "AAAA" not in json.dumps(data)
    assert "run_image" in json.dumps(data)
    assert "not interpreted" in json.dumps(data)


def test_summary_admission_uses_the_new_provider_calibration():
    model = cast(
        Model,
        SimpleNamespace(ref="test/model", limit={"context": 16000, "output": 4096}),
    )
    ref = RunRef("run_a")
    unit = experiment.HistoryUnit(ref, "succeeded", (Message.user("history" * 50),))
    reducer = experiment.Compaction(
        (ref,), lambda _: unit, model, size=1024, max_output_tokens=4096
    )
    assert reducer.next_call() is not None
    result = ModelCallResult(
        message=Message.assistant("fact" * 1000),
        usage=ModelUsage(input_tokens=reducer.estimate * 2, output_tokens=1000),
    )
    assert result.usage is not None
    assert result.usage.input_tokens < reducer.capacity
    with pytest.raises(experiment.SummaryTooLarge, match="next batch"):
        reducer.validate_summary(result)
    assert reducer.summary == ""


@pytest.mark.parametrize(
    "caller_scale,compact_scale,error", [(5, 1, "caller"), (1, 5, "next batch")]
)
def test_summary_is_counted_separately_for_both_models(
    monkeypatch, caller_scale, compact_scale, error
):
    from toolang.base.types.run import ModelCall

    monkeypatch.setattr(
        tokens,
        "_MODEL_TOKEN_SCALES",
        {"test/caller": caller_scale, "test/reducer": compact_scale},
    )
    caller = tokens.TokenCounter("test/caller")
    ref = RunRef("run_a")
    unit = experiment.HistoryUnit(ref, "succeeded", (Message.user("old fact"),))
    model = cast(
        Model,
        SimpleNamespace(ref="test/reducer", limit={"context": 18000, "output": 2048}),
    )
    reducer = experiment.Compaction(
        (ref,),
        lambda _: unit,
        model,
        size=256,
        max_output_tokens=2048,
        summary_fits=lambda summary: (
            caller.base(ModelCall("", [Message.user(summary)])) <= 6000
        ),
    )
    assert reducer.next_call() is not None
    oversized = ModelCallResult(message=Message.assistant("fact " * 500))
    with pytest.raises(experiment.SummaryTooLarge, match=error):
        reducer.accept(oversized)
    assert reducer.summary == ""
    assert [unit.ref for unit in reducer.batch] == [ref]
    reducer.accept(ModelCallResult(message=Message.assistant("short fact")))
    assert reducer.summary == "short fact"
