"""Recovery classification through real SDK and HTTP streaming decoders."""

import asyncio
import json
from collections.abc import Callable

import httpx
from openai import AsyncOpenAI
import pytest

from toolang.base.errors import ModelResponseError
from toolang.base.types.model import Model, ModelRoute, ModelToolang
from toolang.base.types.run import ModelCall, ModelCallResult
from toolang.plugin.adapters import (
    chat_completions,
    generate_content,
    messages,
    responses,
)

MODULES = {
    "chat_completions": chat_completions,
    "responses": responses,
    "messages": messages,
    "generate_content": generate_content,
}


@pytest.fixture
def wire_call(monkeypatch) -> Callable[..., ModelCallResult]:
    client_type = httpx.AsyncClient

    def call(
        protocol,
        events,
        *,
        status=200,
        stream=True,
        disconnect=False,
        observer_error=None,
        truncated=False,
    ):
        async def scenario():
            body = (
                "".join(f"data: {json.dumps(event)}\n\n" for event in events)
                if status == 200 and stream
                else json.dumps(events)
            )

            if truncated:
                body += 'data: {"unterminated":\n\n'

            class Body(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield body.encode()
                    if disconnect:
                        raise httpx.ReadError("connection lost after response data")

            client = client_type(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        status,
                        headers={
                            "content-type": "text/event-stream"
                            if status == 200 and stream
                            else "application/json"
                        },
                        stream=Body(),
                    )
                )
            )
            module = MODULES[protocol]
            if protocol in {"chat_completions", "responses"}:
                sdk = AsyncOpenAI(
                    api_key="test",
                    base_url="https://example.invalid",
                    http_client=client,
                    max_retries=0,
                )
                monkeypatch.setattr(
                    module, "create_client", lambda *args, **kwargs: sdk
                )
            else:
                monkeypatch.setattr(httpx, "AsyncClient", lambda: client)
            adapter = module.create_model_adapter({})
            model = Model(
                id="test",
                name="Test",
                _toolang=ModelToolang(
                    provider="test",
                    route=ModelRoute(
                        adapter=protocol, api="https://example.invalid", env=()
                    ),
                ),
            )

            async def ignore(event):
                if observer_error is not None:
                    raise observer_error

            try:
                request = ModelCall(
                    instructions="", messages=[], max_output_tokens=1024
                )
                if stream:
                    return await adapter.stream(
                        model, request, environ={}, on_event=ignore
                    )
                return await adapter.invoke(model, request, environ={})
            finally:
                await client.aclose()

        return asyncio.run(scenario())

    return call


@pytest.mark.parametrize(
    ("protocol", "error"),
    [
        ("chat_completions", {"type": "server_error", "code": "server_error"}),
        ("responses", {"code": "rate_limit_exceeded"}),
        ("messages", {"type": "overloaded_error"}),
        ("generate_content", {"code": 503, "status": "UNAVAILABLE"}),
    ],
)
def test_transient_in_band_error_is_recoverable(wire_call, protocol, error):
    with pytest.raises(ModelResponseError) as caught:
        wire_call(protocol, [{"type": "error", "error": error}])
    assert caught.value.kind == "transport_error"


@pytest.mark.parametrize("protocol", ["messages", "generate_content"])
def test_native_early_eof_never_returns_success(wire_call, protocol):
    event = (
        {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "partial"},
        }
        if protocol == "messages"
        else {"candidates": [{"content": {"parts": [{"text": "partial"}]}}]}
    )
    with pytest.raises(ModelResponseError) as caught:
        wire_call(protocol, [event])
    assert caught.value.kind == "incomplete_stream"
    assert caught.value.partial_text == "partial"


@pytest.mark.parametrize("protocol", ["messages", "generate_content"])
def test_streaming_http_quota_exhaustion_stays_terminal(wire_call, protocol):
    with pytest.raises(httpx.HTTPStatusError):
        wire_call(protocol, {"error": {"code": "insufficient_quota"}}, status=429)


def test_messages_stream_uses_argument_deltas_instead_of_empty_start_input(wire_call):
    result = wire_call(
        "messages",
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "call",
                    "name": "lookup",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"path":"/important"}',
                },
            },
            {"type": "message_stop"},
        ],
    )
    assert result.tool_calls[0].input == {"path": "/important"}


@pytest.mark.parametrize("protocol", ["messages", "generate_content"])
@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize(
    ("arguments", "kind"),
    [
        ("", None),
        ('{"x":', "invalid_json"),
        ("[]", "non_object_arguments"),
        ("{}", None),
    ],
)
def test_native_arguments_share_strict_validation(
    wire_call, protocol, stream, arguments, kind
):
    if protocol == "messages":
        block = {"type": "tool_use", "id": "call", "name": "lookup", "input": arguments}
        data = (
            [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {**block, "input": {}},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": arguments},
                },
                {
                    "type": "message_delta",
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                    "delta": {"stop_reason": "tool_use"},
                },
                {"type": "message_stop"},
            ]
            if stream
            else {
                "content": [block],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            }
        )
    else:
        # Gemini carries an object rather than an encoded JSON string.
        args = [] if kind == "non_object_arguments" else arguments if kind else {}
        data = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"functionCall": {"name": "lookup", "args": args}}]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3},
        }
        data = [data] if stream else data
    if kind is None:
        assert wire_call(protocol, data, stream=stream).tool_calls[0].input == {}
    else:
        with pytest.raises(ModelResponseError) as caught:
            wire_call(protocol, data, stream=stream)
        assert caught.value.kind == kind
        assert caught.value.usage is not None
        assert caught.value.usage.input_tokens == 2
        assert caught.value.usage.output_tokens == 3


@pytest.mark.parametrize("protocol", list(MODULES))
def test_explicit_rejection_never_enters_recovery(wire_call, protocol):
    if protocol == "chat_completions":
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "refusal": "Refused",
                    },
                    "finish_reason": "stop",
                    "index": 0,
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }
    elif protocol == "responses":
        data = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "refusal", "refusal": "Refused"}],
                }
            ],
            "usage": {"input_tokens": 2, "output_tokens": 3},
        }
    elif protocol == "messages":
        data = {
            "stop_reason": "refusal",
            "content": [],
            "usage": {"input_tokens": 2, "output_tokens": 3},
        }
    else:
        data = {
            "candidates": [{"finishReason": "SAFETY"}],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3},
        }
    with pytest.raises(ModelResponseError) as caught:
        wire_call(protocol, data, stream=False)
    assert caught.value.kind == "provider_rejection"
    assert caught.value.usage is not None
    assert caught.value.usage.input_tokens == 2
    assert caught.value.usage.output_tokens == 3


@pytest.mark.parametrize("status", ["incomplete", "failed"])
def test_responses_sdk_preserves_terminal_failure_usage(wire_call, status):
    response = {
        "id": "resp",
        "object": "response",
        "status": "in_progress",
        "output": [],
        "created_at": 1,
    }
    terminal = {
        **response,
        "status": status,
        "incomplete_details": {"reason": "max_output_tokens"},
        "error": {"code": "server_error"} if status == "failed" else None,
        "usage": {"input_tokens": 2, "output_tokens": 3},
    }
    with pytest.raises(ModelResponseError) as caught:
        wire_call(
            "responses",
            [
                {
                    "type": "response.created",
                    "response": response,
                    "sequence_number": 0,
                },
                {
                    "type": f"response.{status}",
                    "response": terminal,
                    "sequence_number": 1,
                },
            ],
        )
    assert caught.value.kind == (
        "output_limit" if status == "incomplete" else "transport_error"
    )
    assert caught.value.usage is not None
    assert caught.value.usage.input_tokens == 2
    assert caught.value.usage.output_tokens == 3


@pytest.mark.parametrize("protocol", list(MODULES))
@pytest.mark.parametrize("ending", ["disconnect", "broken_json"])
def test_terminal_response_survives_later_disconnect(wire_call, protocol, ending):
    if protocol == "chat_completions":
        data = [
            {
                "choices": [
                    {"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}
                ]
            }
        ]
    elif protocol == "responses":
        response = {
            "id": "resp",
            "object": "response",
            "status": "in_progress",
            "output": [],
            "created_at": 1,
        }
        data = [
            {"type": "response.created", "response": response, "sequence_number": 0},
            {
                "type": "response.completed",
                "sequence_number": 1,
                "response": {
                    **response,
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "done",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            },
        ]
    elif protocol == "messages":
        data = [
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "done"},
            },
            {"type": "message_stop"},
        ]
    else:
        data = [
            {
                "candidates": [
                    {"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}
                ]
            }
        ]
    result = wire_call(
        protocol,
        data,
        disconnect=ending == "disconnect",
        truncated=ending == "broken_json",
    )
    assert result.message is not None
    assert result.message.parts[0].text == "done"


@pytest.mark.parametrize(
    "protocol", ["chat_completions", "messages", "generate_content"]
)
def test_observer_failure_is_not_a_provider_retry(wire_call, protocol):
    if protocol == "chat_completions":
        data = [
            {
                "choices": [
                    {"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}
                ]
            }
        ]
    elif protocol == "messages":
        data = [
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "done"},
            },
            {"type": "message_stop"},
        ]
    else:
        data = [
            {
                "candidates": [
                    {"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}
                ]
            }
        ]
    error = httpx.ReadError("observer connection failed")
    with pytest.raises(httpx.ReadError) as caught:
        wire_call(protocol, data, observer_error=error)
    assert caught.value is error


@pytest.mark.parametrize("protocol", ["messages", "generate_content"])
def test_known_http_rejection_wins_over_error_body_disconnect(wire_call, protocol):
    with pytest.raises(httpx.HTTPStatusError) as caught:
        wire_call(
            protocol,
            {"error": {"code": "invalid_api_key"}},
            status=401,
            disconnect=True,
        )
    assert caught.value.response.status_code == 401


@pytest.mark.parametrize("protocol", list(MODULES))
def test_broken_json_event_is_an_incomplete_response(wire_call, protocol):
    with pytest.raises(ModelResponseError) as caught:
        wire_call(protocol, [], truncated=True)
    assert caught.value.kind == "incomplete_stream"
