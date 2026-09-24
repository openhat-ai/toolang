"""Existing human projections exclude native reasoning at every nesting level."""

import json

import pytest

from toolang.base.types.message import ReasoningPart, TextPart, ToolCallPart
from toolang.cli.common.human_values import human_scalar_text, parts_response_text
from toolang.execution.types import Local
from toolang.lang.types import Array, Struct
from toolang.execution.values import parts_from_local


def test_human_fallbacks_hide_reasoning_and_native_fields():
    origin: dict[str, object] = {"adapter": "generate_content", "model": "model"}
    reasoning = ReasoningPart("private reasoning", "opaque", "provider", origin)
    call = ToolCallPart(
        "call",
        "tool",
        "tool",
        signature="tool-signature",
        provider="provider",
        provider_metadata=origin,
    )
    text = TextPart(
        "answer",
        signature="text-signature",
        provider="provider",
        provider_metadata=origin,
    )
    assert parts_response_text((reasoning, text)) == "answer"
    assert parts_response_text((reasoning,)) == ""
    assert human_scalar_text(reasoning, "ReasoningPart") == ""
    fallback = json.loads(parts_response_text((reasoning, call)))
    assert fallback == [
        {
            "type": "tool_call",
            "tool_call_id": "call",
            "tool_name": "tool",
            "tool_family": "tool",
            "input": {},
        }
    ]
    local = Local(
        Struct(
            "Result",
            {
                "nested": Array(
                    "Part[][]", (Array("Part[]", (reasoning, text, call)),)
                ),
                "reasoning": reasoning,
            },
        )
    )
    raw = parts_from_local(local)
    visible = parts_from_local(local, content_only=True)
    assert isinstance(raw[0], TextPart)
    assert isinstance(visible[0], TextPart)
    assert "private reasoning" in raw[0].text
    assert "opaque" in raw[0].text
    assert json.loads(visible[0].text) == {
        "nested": [
            [{"$part": {"type": "text", "text": "answer"}}, {"$part": fallback[0]}]
        ]
    }


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("run_id", ["run_result", None])
def test_chat_result_filters_native_parts_before_structured_fallback(remote, run_id):
    import asyncio
    from contextlib import nullcontext
    from types import SimpleNamespace as NS
    from typing import cast
    from toolang.cli.toolang.commands.chat.local import LocalChatSession
    from toolang.cli.toolang.commands.chat.remote import RemoteChatSession
    from toolang.execution.types import Output

    native: dict[str, object] = {"adapter": "responses", "model": "test"}
    output = Output(
        Local(
            Struct(
                "Result",
                {
                    "nested": Array(
                        "Part[]",
                        (
                            ReasoningPart(
                                "hidden thought", "opaque-state", "provider", native
                            ),
                            TextPart(
                                "answer",
                                signature="text-signature",
                                provider="provider",
                                provider_metadata=native,
                            ),
                        ),
                    )
                },
            )
        ),
        "_",
    )
    if remote:

        async def detail(*args, **kwargs):
            return NS(id="run_result", output=output)

        session = cast(RemoteChatSession, NS(_run_detail=detail))
        result = asyncio.run(
            RemoteChatSession._get_result(session, run_id, thread_id="term_result")
        )
    else:
        session = cast(
            LocalChatSession,
            NS(
                history=NS(
                    get_output=lambda _: output,
                    thread_view=lambda _: NS(
                        runs=lambda **_: [
                            NS(id="run_result", status="succeeded", output=output)
                        ]
                    ),
                ),
                store=NS(read_transaction=nullcontext),
            ),
        )
        result = LocalChatSession.get_result(session, run_id, thread_id="term_result")
    rendered = parts_response_text(result.output)
    assert json.loads(rendered) == {
        "nested": [{"$part": {"type": "text", "text": "answer"}}]
    }
