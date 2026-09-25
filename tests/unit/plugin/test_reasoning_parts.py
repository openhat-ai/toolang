"""Native reasoning survives normalization, streaming, and subsequent requests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace as NS
from typing import Any, cast

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.message import Message, ReasoningPart, TextPart, ToolCallPart
from toolang.base.types.model import Model, ModelRoute, ModelToolang, Reasoning
from toolang.base.types.run import (
    ModelCall,
    ModelPartDelta,
    ModelPartEnd,
    ModelPartStart,
)
from toolang.plugin.adapters import chat_completions as chat
from toolang.plugin.adapters import generate_content as gemini
from toolang.plugin.adapters import messages, responses


def model(adapter, provider=None, options=None):
    providers = {
        "messages": "anthropic",
        "generate_content": "google",
        "responses": "openai",
        "chat_completions": "vercel",
    }
    return Model(
        id="test-model",
        name="Test",
        _toolang=ModelToolang(
            provider=provider or providers[adapter],
            route=ModelRoute(
                adapter=adapter,
                api="https://example.test",
                env=(),
                options=options or {},
            ),
        ),
    )


def sdk(value):
    if isinstance(value, dict):
        return NS(**{key: sdk(item) for key, item in value.items()})
    if isinstance(value, list):
        return [sdk(item) for item in value]
    return value


def native_response(adapter, tools):
    content: list[dict[str, Any]]
    value: dict[str, Any]
    if adapter == "messages":
        content = [
            {"type": "thinking", "thinking": "考察 α\n", "signature": "opaque-one"},
            {"type": "redacted_thinking", "data": "opaque-two"},
            {"type": "text", "text": "answer"},
        ]
        if tools:
            content.append(
                {"type": "tool_use", "id": "call", "name": "lookup", "input": {"x": 1}}
            )
        return {"content": content}
    if adapter == "generate_content":
        content = [
            {"text": "考察 α\n", "thought": True},
            {"text": "answer", "thoughtSignature": "opaque-one"},
            {"text": "", "thoughtSignature": "opaque-two"},
        ]
        if tools:
            content.append(
                {
                    "functionCall": {"id": "call", "name": "lookup", "args": {"x": 1}},
                    "thoughtSignature": "opaque-tool",
                }
            )
        return {"candidates": [{"content": {"parts": content}}]}
    if adapter == "responses":
        content = [
            {
                "type": "reasoning",
                "id": "rs",
                "summary": [
                    {"type": "summary_text", "text": "考察 α\n"},
                    {"type": "summary_text", "text": "second"},
                ],
                "content": [{"type": "reasoning_text", "text": "native text"}],
                "encrypted_content": "opaque-one",
                "status": "completed",
            },
            {
                "type": "message",
                "id": "msg",
                "content": [{"type": "output_text", "text": "answer"}],
            },
        ]
        if tools:
            content.append(
                {
                    "type": "function_call",
                    "id": "fc",
                    "call_id": "call",
                    "name": "lookup",
                    "arguments": '{"x":1}',
                }
            )
        return {"id": "response", "output": content}
    details = [
        {
            "type": "reasoning.summary",
            "summary": "考察 α\n",
            "index": 0,
            "id": "rs",
            "format": "openai-responses-v1",
        },
        {
            "type": "reasoning.encrypted",
            "data": "opaque-one",
            "index": 1,
            "id": "rs",
            "format": "openai-responses-v1",
        },
    ]
    value = {
        "content": "answer",
        "reasoning": "考察 α\n",
        "reasoning_content": "考察 α\n",
        "reasoning_details": details,
    }
    if tools:
        value["tool_calls"] = [
            {"id": "call", "function": {"name": "lookup", "arguments": '{"x":1}'}}
        ]
    return {"choices": [{"message": value}]}


def parse(adapter, value, selected):
    if adapter == "messages":
        return messages.parse_message_response(value, model=selected)
    if adapter == "generate_content":
        return gemini.parse_generate_content(value, model=selected)
    if adapter == "responses":
        return responses.parse_response(
            sdk(value), model=selected, request=ModelCall("", []), stateful=False
        )
    return chat.parse_chat_completion(sdk(value), model=selected)


def encode(adapter, message, selected):
    call = ModelCall("", [message], max_output_tokens=1024)
    if adapter == "messages":
        return cast(
            dict[str, Any], messages.messages_payload(selected, call, stream=False)
        )["messages"][0]["content"]
    if adapter == "generate_content":
        return cast(dict[str, Any], gemini.generate_content_payload(selected, call))[
            "contents"
        ][0]["parts"]
    if adapter == "responses":
        return responses.response_payload(selected, call, stateful=False)["input"]
    return chat.chat_completion_payload(selected, call, stream=False)["messages"][0]


@pytest.mark.parametrize(
    "adapter", ["messages", "generate_content", "responses", "chat_completions"]
)
@pytest.mark.parametrize("tools", [False, True])
def test_native_reasoning_round_trip_without_continuation(adapter, tools):
    selected = model(adapter)
    native = native_response(adapter, tools)
    result = parse(adapter, native, selected)
    assert result.continuation is None
    assert result.message is not None
    restored = Message.from_data(json.loads(json.dumps(result.message.to_data())))
    assert restored == result.message
    assert any(isinstance(part, ReasoningPart) for part in restored.parts)
    assert all(
        not (
            {"text", "signature", "arguments", "data", "summary"}
            & part.provider_metadata.keys()
        )
        for part in restored.parts
        if isinstance(part, ReasoningPart | TextPart | ToolCallPart)
    )
    outgoing = encode(adapter, restored, selected)
    if adapter == "messages":
        assert outgoing == native["content"]
    elif adapter == "generate_content":
        assert outgoing == native["candidates"][0]["content"]["parts"]
    elif adapter == "responses":
        assert outgoing[0] == native["output"][0]
        assert (
            sum(
                part.signature is not None
                for part in restored.parts
                if isinstance(part, ReasoningPart)
            )
            == 1
        )
    else:
        assert (
            outgoing["reasoning_details"]
            == native["choices"][0]["message"]["reasoning_details"]
        )
        assert "reasoning" not in outgoing and "reasoning_content" not in outgoing
    foreign = encode(adapter, restored, replace(selected, id="other-model"))
    assert "opaque" not in json.dumps(foreign)
    assert "考察" not in json.dumps(foreign, ensure_ascii=False)
    assert "answer" in json.dumps(foreign)


@pytest.mark.parametrize(
    "adapter", ["messages", "generate_content", "responses", "chat_completions"]
)
def test_reasoning_usage_alone_does_not_create_a_part(adapter):
    payload = {
        "messages": {"content": [], "usage": {"input_tokens": 2, "output_tokens": 5}},
        "generate_content": {
            "candidates": [{"content": {"parts": []}}],
            "usageMetadata": {
                "promptTokenCount": 2,
                "candidatesTokenCount": 0,
                "thoughtsTokenCount": 5,
            },
        },
        "responses": {
            "output": [],
            "usage": {
                "input_tokens": 2,
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 5},
            },
        },
        "chat_completions": {
            "choices": [{"message": {"content": None}}],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 5,
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        },
    }[adapter]
    result = parse(adapter, payload, model(adapter))
    assert result.message is None or not result.message.parts


class Stream:
    def __init__(self, events, final=None, failure=None):
        self.events, self.final, self.failure = events, final, failure

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def __aiter__(self):
        for event in self.events:
            yield sdk(event)
        if self.failure is not None:
            raise self.failure
        if self.final is not None:
            yield sdk({"type": "response.completed", "response": self.final})
        elif self.events and "choices" in self.events[-1]:
            yield sdk({"choices": [{"delta": {}, "finish_reason": "stop"}]})

    async def close(self):
        return None

    async def get_final_response(self):
        return sdk(self.final)


def mock_sdk(monkeypatch, module, stream):
    async def create(**_):
        return stream

    client = NS(
        chat=NS(completions=NS(create=create)), responses=NS(stream=lambda **_: stream)
    )
    monkeypatch.setattr(module, "create_client", lambda *_, **__: client)


def mock_http(monkeypatch, module, events, failure=None):
    class HTTPStream:
        is_error = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for event in events:
                yield "data: " + json.dumps(event)
            if failure is not None:
                raise failure
            terminal = (
                {"type": "message_stop"}
                if module is messages
                else {"candidates": [{"finishReason": "STOP"}]}
            )
            yield "data: " + json.dumps(terminal)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def stream(self, *_, **__):
            return HTTPStream()

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)


def run_stream(adapter, selected, events):
    async def record(event):
        events.append(event)

    return asyncio.run(
        adapter.stream(
            selected,
            ModelCall("", [], max_output_tokens=1024),
            environ={},
            on_event=record,
        )
    )


def assert_events(events, result):
    assert result.message is not None
    starts = [event.part for event in events if isinstance(event, ModelPartStart)]
    ends = [event for event in events if isinstance(event, ModelPartEnd)]
    assert starts == list(range(len(result.message.parts)))
    assert len(ends) == len(starts) == len({event.part for event in ends})
    assert {event.part: event.data for event in ends} == dict(
        enumerate(result.message.parts)
    )
    for index, part in enumerate(result.message.parts):
        chunks: list[str] = [
            event.delta.text
            for event in events
            if isinstance(event, ModelPartDelta) and event.part == index
        ]
        if chunks and isinstance(part, ReasoningPart | TextPart):
            assert "".join(chunks) == part.text


@pytest.mark.parametrize("provider", ["vercel", "openrouter", "deepseek", "compatible"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_chat_alias_selection_is_call_wide_and_flushes_interrupted_fallback(
    monkeypatch, provider, interrupted
):
    chunks: list[dict[str, Any]] = [
        {"choices": [{"delta": {"reasoning_content": "α", "reasoning": "α"}}]},
        {"choices": [{"delta": {"content": "answer"}}]},
    ]
    if provider != "deepseek":
        chunks.append(
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_details": [
                                {
                                    "type": "reasoning.summary",
                                    "summary": "α",
                                    "index": 0,
                                }
                            ]
                        }
                    }
                ]
            }
        )
    chunks.append(
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_details": [
                            {
                                "type": "reasoning.encrypted",
                                "data": "opaque",
                                "index": 1,
                            }
                        ]
                    }
                }
            ]
        }
    )
    mock_sdk(
        monkeypatch,
        chat,
        Stream(chunks, failure=RuntimeError("disconnected") if interrupted else None),
    )
    events = []
    selected = model("chat_completions", provider)
    if interrupted:
        with pytest.raises(RuntimeError, match="disconnected"):
            run_stream(chat.ChatCompletionsModelAdapter(), selected, events)
        parts = [event.data for event in events if isinstance(event, ModelPartEnd)]
        reason = [part for part in parts if isinstance(part, ReasoningPart)]
        assert [part.text for part in reason] == ["α"]
        assert all(
            part.signature is None
            and part.provider is None
            and part.provider_metadata == {}
            for part in reason
        )
    else:
        result = run_stream(chat.ChatCompletionsModelAdapter(), selected, events)
        assert_events(events, result)
        reason = [
            part for part in result.message.parts if isinstance(part, ReasoningPart)
        ]
        assert [part.text for part in reason if part.text] == ["α"]
        assert [part.signature for part in reason if part.signature] == ["opaque"]


def test_messages_stream_keeps_blocks_separate_and_assembles_signature(monkeypatch):
    events = []
    native = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "考察 α"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig-"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "one"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": "answer"},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "thinking", "thinking": "another"},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "signature_delta", "signature": "sig-two"},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "content_block_start",
            "index": 3,
            "content_block": {
                "type": "tool_use",
                "id": "call",
                "name": "lookup",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 3,
            "delta": {"type": "input_json_delta", "partial_json": '{"x":1}'},
        },
        {"type": "content_block_stop", "index": 3},
    ]
    mock_http(monkeypatch, messages, native)
    result = run_stream(messages.MessagesModelAdapter(), model("messages"), events)
    assert_events(events, result)
    assert [part.type for part in result.message.parts] == [
        "reasoning",
        "text",
        "reasoning",
        "tool_call",
    ]
    assert result.message.parts[0].signature == "sig-one"
    assert result.tool_calls[0].input == {"x": 1}
    assert result.continuation is None


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tools", [False, True])
def test_messages_unsigned_thinking_round_trips_on_compatible_routes(
    monkeypatch, stream, tools
):
    selected = model("messages", provider="vercel")
    content: list[dict[str, Any]] = [
        {"type": "thinking", "thinking": "analysis α\n"},
        {"type": "text", "text": "answer"},
    ]
    native = {"content": content}
    if tools:
        content.append(
            {"type": "tool_use", "id": "call", "name": "lookup", "input": {"x": 1}}
        )
    if stream:
        events = [
            event
            for index, block in enumerate(native["content"])
            for event in (
                {"type": "content_block_start", "index": index, "content_block": block},
                {"type": "content_block_stop", "index": index},
            )
        ]
        mock_http(monkeypatch, messages, events)
        updates = []
        result = run_stream(messages.MessagesModelAdapter(), selected, updates)
        assert_events(updates, result)
    else:
        result = messages.parse_message_response(native, model=selected)
    assert result.message is not None
    restored = Message.from_data(json.loads(json.dumps(result.message.to_data())))
    assert isinstance(restored.parts[0], ReasoningPart)
    assert restored.parts[0].signature is None
    assert encode("messages", restored, selected) == native["content"]


def test_messages_redacted_thinking_still_requires_opaque_data():
    selected = model("messages", provider="vercel")
    result = messages.parse_message_response(
        {"content": [{"type": "redacted_thinking"}]}, model=selected
    )
    assert result.message is not None
    with pytest.raises(ToolangError, match="signature"):
        encode("messages", result.message, selected)


@pytest.mark.parametrize(
    "failure", [RuntimeError("disconnected"), asyncio.CancelledError()]
)
def test_messages_interruption_keeps_only_completed_native_units(monkeypatch, failure):
    native = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "thinking",
                "thinking": "complete",
                "signature": "good",
            },
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "thinking", "thinking": "prefix"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "signature_delta", "signature": "incomplete"},
        },
    ]
    mock_http(monkeypatch, messages, native, failure)
    events = []
    with pytest.raises(type(failure)):
        run_stream(messages.MessagesModelAdapter(), model("messages"), events)
    parts = [event.data for event in events if isinstance(event, ModelPartEnd)]
    assert parts[0].signature == "good"
    assert parts[1] == ReasoningPart("prefix")


def test_gemini_stream_uses_call_local_blocks_and_preserves_signed_boundaries(
    monkeypatch,
):
    native = [
        {"candidates": [{"content": {"parts": [part]}}]}
        for part in [
            {"text": "think", "thought": True},
            {"text": " α", "thought": True},
            {"text": "answer"},
            {"text": "again", "thought": True},
            {"text": "signed one", "thoughtSignature": "one"},
            {"text": "signed two", "thoughtSignature": "two"},
            {"text": "", "thoughtSignature": "empty"},
            {
                "functionCall": {"id": "call", "name": "lookup", "args": {}},
                "thoughtSignature": "tool",
            },
        ]
    ]
    mock_http(monkeypatch, gemini, native)
    events = []
    result = run_stream(
        gemini.GenerateContentModelAdapter(), model("generate_content"), events
    )
    assert_events(events, result)
    assert [
        part.text for part in result.message.parts if isinstance(part, ReasoningPart)
    ] == ["think α", "again"]
    assert [
        part.signature
        for part in result.message.parts
        if isinstance(part, TextPart | ToolCallPart)
    ] == [None, "one", "two", "empty", "tool"]
    outgoing = encode("generate_content", result.message, model("generate_content"))
    assert outgoing[-2] == {"text": "", "thoughtSignature": "empty"}


def test_responses_stream_shared_signature_waits_for_item_done(monkeypatch):
    selected = model("responses")
    native = native_response("responses", True)
    reasoning, message, tool = native["output"]
    frames = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "reasoning",
                "id": "rs",
                "summary": [],
                "encrypted_content": "unfinished",
            },
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs",
            "output_index": 0,
            "summary_index": 0,
            "delta": "考察 ",
        },
        {
            "type": "response.reasoning_summary_text.done",
            "item_id": "rs",
            "output_index": 0,
            "summary_index": 0,
            "text": "考察 α\n",
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs",
            "output_index": 0,
            "summary_index": 1,
            "delta": "second",
        },
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        {
            "type": "response.output_text.delta",
            "item_id": "msg",
            "output_index": 1,
            "content_index": 0,
            "delta": "answer",
        },
        {"type": "response.output_item.done", "output_index": 1, "item": message},
        {"type": "response.output_item.done", "output_index": 2, "item": tool},
    ]
    mock_sdk(monkeypatch, responses, Stream(frames, native))
    events = []
    result = run_stream(responses.ResponsesModelAdapter(), selected, events)
    assert_events(events, result)
    reason = [part for part in result.message.parts if isinstance(part, ReasoningPart)]
    assert [part.signature for part in reason] == ["opaque-one", None, None]
    assert encode("responses", result.message, selected)[0] == reasoning
    assert set(result.continuation) == {
        "previous_response_id",
        "baseline_count",
        "prefix",
    }


def test_responses_interrupted_summary_does_not_keep_partial_encryption(monkeypatch):
    frames = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "reasoning",
                "id": "rs",
                "encrypted_content": "unfinished",
            },
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs",
            "summary_index": 0,
            "delta": "prefix",
        },
    ]
    mock_sdk(
        monkeypatch, responses, Stream(frames, failure=RuntimeError("disconnected"))
    )
    events = []
    with pytest.raises(RuntimeError):
        run_stream(responses.ResponsesModelAdapter(), model("responses"), events)
    assert [event.data for event in events if isinstance(event, ModelPartEnd)] == [
        ReasoningPart("prefix")
    ]


def test_responses_rejects_contradictory_final_snapshot(monkeypatch):
    final = {
        "id": "response",
        "output": [
            {
                "type": "reasoning",
                "id": "rs",
                "summary": [{"type": "summary_text", "text": "different"}],
            }
        ],
    }
    frames = [
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs",
            "summary_index": 0,
            "delta": "prefix",
        }
    ]
    mock_sdk(monkeypatch, responses, Stream(frames, final))
    with pytest.raises(ToolangError, match="contradicts"):
        run_stream(responses.ResponsesModelAdapter(), model("responses"), [])


def test_explicit_reasoning_visibility_options_survive_normalized_effort():
    request = ModelCall("", [], reasoning=Reasoning("high"), max_output_tokens=1024)
    response = responses.response_payload(
        model(
            "responses",
            options={
                "reasoning": {"summary": "auto"},
                "include": ["reasoning.encrypted_content"],
            },
        ),
        request,
        stateful=False,
    )
    assert response["reasoning"] == {"effort": "high", "summary": "auto"}
    assert response["include"] == ["reasoning.encrypted_content"]
    message = messages.messages_payload(
        model("messages", options={"thinking": {"display": "summarized"}}),
        request,
        stream=False,
    )
    assert message["thinking"] == {"type": "adaptive", "display": "summarized"}
    generated: Any = gemini.generate_content_payload(
        model(
            "generate_content",
            options={"generationConfig": {"thinkingConfig": {"includeThoughts": True}}},
        ),
        request,
    )
    assert cast(dict[str, Any], generated["generationConfig"])["thinkingConfig"] == {
        "thinkingLevel": "HIGH",
        "includeThoughts": True,
    }


@pytest.mark.parametrize("failure", [False, True])
def test_chat_final_snapshots_reconcile_tools_and_preserve_completed_reasoning(
    monkeypatch, failure
):
    detail = {
        "type": "reasoning.text",
        "text": "thought",
        "index": 0,
        "signature": "opaque",
    }
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_details": [detail],
                        "content": "ans",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call",
                                "function": {"name": "lookup", "arguments": '{"x":'},
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "reasoning_details": [detail],
                        "content": "answer",
                        "tool_calls": [
                            {
                                "id": "call",
                                "function": {"name": "lookup", "arguments": '{"x":1}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]
    mock_sdk(
        monkeypatch,
        chat,
        Stream(chunks, failure=RuntimeError("usage disconnected") if failure else None),
    )
    events = []
    if failure:
        with pytest.raises(RuntimeError, match="usage disconnected"):
            run_stream(
                chat.ChatCompletionsModelAdapter(), model("chat_completions"), events
            )
        (ended,) = [
            event.data
            for event in events
            if isinstance(event, ModelPartEnd) and isinstance(event.data, ReasoningPart)
        ]
        assert ended.text == "thought" and ended.signature == "opaque"
    else:
        result = run_stream(
            chat.ChatCompletionsModelAdapter(), model("chat_completions"), events
        )
        assert_events(events, result)
        assert result.tool_calls[0].input == {"x": 1}
        assert result.message.parts[0].text == "thought"


def test_incomplete_opaque_responses_item_does_not_keep_partial_signature():
    result = responses.parse_response(
        sdk(
            {
                "output": [
                    {
                        "type": "reasoning",
                        "id": "rs",
                        "summary": [],
                        "encrypted_content": "partial",
                        "status": "incomplete",
                    }
                ]
            }
        ),
        model=model("responses"),
        request=ModelCall("", []),
        stateful=False,
    )
    assert result.message is None or not result.message.parts


@pytest.mark.parametrize(
    "mutation", ["drop-owner", "drop-block", "bad-index", "bad-field"]
)
def test_responses_requires_complete_native_reasoning_groups(mutation):
    selected = model("responses")
    result = parse("responses", native_response("responses", False), selected)
    assert result.message is not None
    parts = list(result.message.parts)
    if mutation == "drop-owner":
        del parts[0]
    elif mutation == "drop-block":
        del parts[1]
    else:
        part = parts[1]
        assert isinstance(part, ReasoningPart)
        parts[1] = replace(
            part,
            provider_metadata={
                **part.provider_metadata,
                "index" if mutation == "bad-index" else "field": "invalid",
            },
        )
    with pytest.raises(ToolangError, match="incomplete|malformed"):
        encode("responses", Message("assistant", tuple(parts)), selected)


def test_responses_does_not_group_native_ids_across_messages():
    selected = model("responses")
    result = parse("responses", native_response("responses", False), selected)
    assert result.message is not None
    payload = responses.response_payload(
        selected,
        ModelCall("", [result.message, Message.user("again"), result.message]),
        stateful=False,
    )
    reasoning = [item for item in payload["input"] if item.get("type") == "reasoning"]
    assert len(reasoning) == 2 and reasoning[0] == reasoning[1]


def test_chat_same_index_distinct_native_ids_preserve_multiple_reasoning_details():
    reasoning = chat._ChatReasoning(model("chat_completions"))
    first = {
        "type": "reasoning.summary",
        "id": "rs-first",
        "index": 0,
        "summary": "first thought",
    }
    second = {
        "type": "reasoning.summary",
        "id": "rs-second",
        "index": 0,
        "summary": "second thought",
    }

    reasoning.add({"reasoning_details": [first]})
    reasoning.add({"reasoning_details": [second]})

    result = reasoning.values()
    assert [(part.text, part.provider_metadata["id"]) for _, part in result] == [
        ("first thought", "rs-first"),
        ("second thought", "rs-second"),
    ]


@pytest.mark.parametrize("streaming", [False, True])
def test_chat_reasoning_details_without_ids_use_type_scoped_fallback(
    monkeypatch, streaming
):
    selected = model("chat_completions")
    if streaming:
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_details": [
                                {
                                    "type": "reasoning.summary",
                                    "index": 0,
                                    "summary": "first ",
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_details": [
                                {
                                    "type": "reasoning.summary",
                                    "index": 1,
                                    "summary": "second",
                                }
                            ]
                        }
                    }
                ]
            },
        ]
        mock_sdk(monkeypatch, chat, Stream(chunks))
        events = []
        result = run_stream(chat.ChatCompletionsModelAdapter(), selected, events)
        assert_events(events, result)
    else:
        result = parse(
            "chat_completions",
            {
                "choices": [
                    {
                        "message": {
                            "reasoning_details": [
                                {"type": "reasoning.summary", "summary": "first second"}
                            ]
                        }
                    }
                ]
            },
            selected,
        )

    assert result.message is not None
    (part,) = [part for part in result.message.parts if isinstance(part, ReasoningPart)]
    assert part.text == "first second"
    assert "id" not in part.provider_metadata
    if not streaming:
        assert "index" not in part.provider_metadata
    else:
        assert part.provider_metadata["index"] == 1


def test_chat_missing_id_does_not_guess_between_multiple_native_ids():
    reasoning = chat._ChatReasoning(model("chat_completions"))
    for native_id, text in (("rs-a", "alpha"), ("rs-b", "beta")):
        reasoning.add(
            {
                "reasoning_details": [
                    {
                        "type": "reasoning.summary",
                        "id": native_id,
                        "summary": text,
                    }
                ]
            }
        )

    reasoning.add(
        {
            "reasoning_details": [
                {"type": "reasoning.summary", "summary": "idless-fragment"}
            ]
        }
    )

    parts = [part for _, part in reasoning.values()]
    assert [part.text for part in parts] == ["alpha", "beta", "idless-fragment"]
    assert [part.provider_metadata.get("id") for part in parts] == [
        "rs-a",
        "rs-b",
        None,
    ]


def test_chat_reasoning_detail_does_not_guess_owner_when_later_delta_omits_id(
    monkeypatch,
):
    selected = model("chat_completions")
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_details": [
                            {
                                "type": "reasoning.summary",
                                "id": "rs-first",
                                "index": 0,
                                "summary": "first ",
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_details": [
                            {
                                "type": "reasoning.summary",
                                "index": 1,
                                "summary": "second",
                            }
                        ]
                    }
                }
            ]
        },
    ]
    mock_sdk(monkeypatch, chat, Stream(chunks))
    events = []

    result = run_stream(chat.ChatCompletionsModelAdapter(), selected, events)
    assert_events(events, result)

    assert result.message is not None
    parts = [part for part in result.message.parts if isinstance(part, ReasoningPart)]
    assert [part.text for part in parts] == ["first ", "second"]
    assert [part.provider_metadata.get("id") for part in parts] == ["rs-first", None]


def test_chat_reasoning_detail_id_wins_over_changed_sequence_index():
    reasoning = chat._ChatReasoning(model("chat_completions"))
    reasoning.add(
        {
            "reasoning_details": [
                {
                    "type": "reasoning.summary",
                    "id": "rs-stable",
                    "index": 0,
                    "summary": "first ",
                }
            ]
        }
    )
    reasoning.add(
        {
            "reasoning_details": [
                {
                    "type": "reasoning.summary",
                    "id": "rs-stable",
                    "index": 1,
                    "summary": "second",
                }
            ]
        }
    )

    key, part = reasoning.values()[0]
    assert key == ("reasoning", ("id", "rs-stable", "reasoning.summary"))
    assert part.text == "first second"
    assert part.provider_metadata["id"] == "rs-stable"
    assert part.provider_metadata["index"] == 1


def test_chat_reasoning_details_can_share_idless_fallbacks_with_id_blocks():
    reasoning = chat._ChatReasoning(model("chat_completions"))
    reasoning.add(
        {
            "reasoning_details": [
                {"type": "reasoning.summary", "id": "rs-main", "summary": "identified"}
            ]
        }
    )
    reasoning.add(
        {"reasoning_details": [{"type": "reasoning.summary", "summary": "fallback"}]}
    )

    parts = [part for _, part in reasoning.values()]
    assert [part.text for part in parts] == ["identified", "fallback"]


def test_chat_reasoning_detail_id_with_different_types_keeps_native_parts_distinct():
    reasoning = chat._ChatReasoning(model("chat_completions"))
    reasoning.add(
        {
            "reasoning_details": [
                {
                    "type": "reasoning.summary",
                    "id": "rs-unique",
                    "summary": "summary",
                }
            ]
        }
    )

    reasoning.add(
        {
            "reasoning_details": [
                {
                    "type": "reasoning.encrypted",
                    "id": "rs-unique",
                    "data": "ciphertext",
                }
            ]
        }
    )

    parts = [part for _, part in reasoning.values()]
    assert [(part.text, part.signature) for part in parts] == [
        ("summary", None),
        ("", "ciphertext"),
    ]


def test_chat_reasoning_detail_adopts_id_when_it_appears_after_idless_delta(
    monkeypatch,
):
    selected = model("chat_completions")
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_details": [
                            {"type": "reasoning.summary", "summary": "first "}
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_details": [
                            {
                                "type": "reasoning.summary",
                                "id": "rs-late",
                                "summary": "second",
                            }
                        ]
                    }
                }
            ]
        },
    ]
    mock_sdk(monkeypatch, chat, Stream(chunks))
    events = []

    result = run_stream(chat.ChatCompletionsModelAdapter(), selected, events)
    assert_events(events, result)

    assert result.message is not None
    (part,) = [part for part in result.message.parts if isinstance(part, ReasoningPart)]
    assert part.text == "first second"
    assert part.provider_metadata["id"] == "rs-late"


def test_chat_late_opaque_detail_keeps_its_native_sequence(monkeypatch):
    selected = model("chat_completions")
    readable = {"type": "reasoning.summary", "summary": "thought", "index": 1}
    opaque = {"type": "reasoning.encrypted", "data": "opaque", "index": 0}
    chunks = [
        {"choices": [{"delta": {"reasoning_details": [detail]}}]}
        for detail in (readable, opaque)
    ]
    mock_sdk(monkeypatch, chat, Stream(chunks))
    events = []
    result = run_stream(chat.ChatCompletionsModelAdapter(), selected, events)
    assert_events(events, result)
    assert result.message is not None
    assert encode("chat_completions", result.message, selected)[
        "reasoning_details"
    ] == [opaque, readable]


@pytest.mark.parametrize("streaming", [False, True])
def test_chat_shared_index_keeps_readable_and_encrypted_details(monkeypatch, streaming):
    selected = model("chat_completions")
    common = {"id": "rs", "index": 0, "format": "openai-responses-v1"}
    details = [
        {**common, "type": "reasoning.summary", "summary": "first second"},
        {**common, "type": "reasoning.encrypted", "data": "opaque-state"},
    ]
    if streaming:
        chunks: list[dict[str, Any]] = [
            {"choices": [{"delta": {"reasoning_details": [detail]}}]}
            for detail in (
                {**details[0], "summary": "first "},
                {**details[1], "data": "opaque-"},
                {**details[0], "summary": "second"},
                {**details[1], "data": "state"},
            )
        ]
        chunks.append(
            {
                "choices": [
                    {"message": {"reasoning_details": details}, "finish_reason": "stop"}
                ]
            }
        )
        mock_sdk(monkeypatch, chat, Stream(chunks))
        events = []
        result = run_stream(chat.ChatCompletionsModelAdapter(), selected, events)
        assert_events(events, result)
    else:
        result = parse(
            "chat_completions",
            {"choices": [{"message": {"reasoning_details": details}}]},
            selected,
        )
    assert result.message is not None
    restored = Message.from_data(json.loads(json.dumps(result.message.to_data())))
    readable, opaque = restored.parts
    assert isinstance(readable, ReasoningPart) and isinstance(opaque, ReasoningPart)
    assert (readable.text, readable.signature) == ("first second", None)
    assert (opaque.text, opaque.signature) == ("", "opaque-state")
    assert (
        encode("chat_completions", restored, selected)["reasoning_details"] == details
    )


@pytest.mark.parametrize("first_field", ["content", "summary"])
def test_responses_native_owner_does_not_depend_on_observation_order(
    monkeypatch, first_field
):
    selected = model("responses")
    native = native_response("responses", False)
    item = native["output"][0]
    first_index = 0 if first_field == "content" else 1
    first_text = "native text" if first_field == "content" else "second"
    frames = [
        {
            "type": "response.reasoning_text.delta"
            if first_field == "content"
            else "response.reasoning_summary_text.delta",
            "item_id": "rs",
            "output_index": 0,
            "content_index"
            if first_field == "content"
            else "summary_index": first_index,
            "delta": first_text,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
    ]
    mock_sdk(monkeypatch, responses, Stream(frames, native))
    events = []
    result = run_stream(responses.ResponsesModelAdapter(), selected, events)
    assert_events(events, result)
    assert result.message.parts[0].text == first_text
    restored = Message.from_data(json.loads(json.dumps(result.message.to_data())))
    assert encode("responses", restored, selected)[0] == item


@pytest.mark.parametrize("cancel_at", ["start", "delta"])
def test_completed_native_part_survives_observer_cancellation(cancel_at):
    from toolang.plugin.adapters._parts import PartStream

    expected = ReasoningPart(
        "complete", "signature", "provider", {"adapter": "messages", "model": "model"}
    )
    events = []
    interrupted = False

    async def record(event):
        nonlocal interrupted
        events.append(event)
        target = ModelPartStart if cancel_at == "start" else ModelPartDelta
        if isinstance(event, target) and not interrupted:
            interrupted = True
            raise asyncio.CancelledError()

    async def scenario():
        parts = PartStream(record)
        if cancel_at == "delta":
            await parts.start("native", ReasoningPart(""))
        with pytest.raises(asyncio.CancelledError):
            await parts.finish("native", expected)
        await parts.interrupt()
        assert parts.message().parts == (expected,)
        assert [e.data for e in events if isinstance(e, ModelPartEnd)] == [expected]

    asyncio.run(scenario())


@pytest.mark.parametrize("streaming", [False, True])
def test_responses_accepts_reasoning_with_null_optional_content(monkeypatch, streaming):
    selected = model("responses")
    native = native_response("responses", False)
    native["output"][0]["content"] = None
    if streaming:
        mock_sdk(monkeypatch, responses, Stream([], native))
        result = run_stream(responses.ResponsesModelAdapter(), selected, [])
    else:
        result = parse("responses", native, selected)
    assert result.message is not None
    assert result.message.parts[0].signature == "opaque-one"
    assert (
        encode("responses", result.message, selected)[0]["summary"]
        == native["output"][0]["summary"]
    )
