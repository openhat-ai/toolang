from __future__ import annotations

from toolang.concepts.messages import (
    FilePart,
    TextPart,
    ToolPart,
    TurnMessage,
)
from toolang.runtime.chat_protocol import (
    AIMessageChunkEncoder,
    TurnMessageBuilder,
    chunk_to_dict,
)
from toolang.runtime.model_exec import (
    TextDeltaEvent,
    ToolInputAvailableEvent,
    ToolInputDeltaEvent,
    ToolInputStartEvent,
    ToolOutputAvailableEvent,
)
from toolang.concepts.tools import ToolCallResult


def test_turn_message_round_trips_with_ordered_parts() -> None:
    message = TurnMessage(
        id="m1",
        role="assistant",
        parts=(
            TextPart(id="m1:text:1", text="Listed files."),
            ToolPart(
                id="m1:tool:2",
                tool_call_id="call_1",
                tool_name="filesystem",
                tool_family="filesystem",
                state="output-available",
                input={"action": "list_dir", "path": "."},
                output={"entries": ["README.md"]},
            ),
            FilePart(
                id="m1:file:3",
                file_id="file_1",
                media_type="text/plain",
                name="report.txt",
                uri="file:///tmp/report.txt",
            ),
        ),
        created_at="2026-03-26T10:00:00Z",
        metadata={"thread": "owner"},
        provider_metadata={"model": "gpt-5"},
    )

    loaded = TurnMessage.from_dict(message.to_dict())

    assert loaded == message
    assert loaded.preview_text() == "Listed files."


def test_turn_message_builder_preserves_text_tool_text_order() -> None:
    builder = TurnMessageBuilder(message_id="m2")

    builder.append_text_delta("Before tool. ")
    builder.tool_input_start("call_1")
    builder.tool_input_delta("call_1", '{"command":"pwd"}')
    builder.tool_input_available(
        tool_call_id="call_1",
        tool_name="shell",
        tool_family="shell",
        input={"command": "pwd"},
    )
    builder.tool_output_available(
        tool_call_id="call_1",
        tool_name="shell",
        tool_family="shell",
        output={"stdout": "/tmp/alice"},
    )
    builder.append_text_delta("After tool.")

    message = builder.build()

    assert isinstance(message.parts[0], TextPart)
    assert isinstance(message.parts[1], ToolPart)
    assert isinstance(message.parts[2], TextPart)
    assert [part.type for part in message.parts] == ["text", "tool", "text"]
    assert message.parts[0].text == "Before tool. "
    assert message.parts[1].tool_call_id == "call_1"
    assert message.parts[1].state == "output-available"
    assert message.parts[1].input == {"command": "pwd"}
    assert message.parts[1].output == {"stdout": "/tmp/alice"}
    assert message.parts[2].text == "After tool."


def test_ai_sdk_chunk_encoder_uses_camel_case_tool_fields() -> None:
    encoder = AIMessageChunkEncoder(message_id="turn_1")

    chunks = []
    chunks.extend(encoder.start())
    chunks.extend(encoder.encode_event(ToolInputStartEvent(tool_call_id="call_1")))
    chunks.extend(
        encoder.encode_event(
            ToolInputDeltaEvent(
                tool_call_id="call_1",
                delta='{"command":"pwd"}',
            )
        )
    )
    chunks.extend(
        encoder.encode_event(
            ToolInputAvailableEvent(
                tool_call_id="call_1",
                family="shell",
                name="shell",
                arguments={"command": "pwd"},
            )
        )
    )
    chunks.extend(
        encoder.encode_event(
            ToolOutputAvailableEvent(
                tool_call_id="call_1",
                result=ToolCallResult(
                    family="shell",
                    name="shell",
                    arguments={"command": "pwd"},
                    output={"stdout": "/tmp/alice"},
                    error=None,
                ),
            )
        )
    )
    chunks.extend(encoder.encode_event(TextDeltaEvent(delta="done")))
    chunks.extend(encoder.finish())

    serialized = [chunk_to_dict(item) for item in chunks]

    assert serialized[0] == {"type": "start"}
    assert serialized[1] == {
        "type": "tool-input-start",
        "id": "turn_1",
        "toolCallId": "call_1",
    }
    assert serialized[2] == {
        "type": "tool-input-delta",
        "id": "turn_1",
        "toolCallId": "call_1",
        "inputTextDelta": '{"command":"pwd"}',
    }
    assert serialized[3]["type"] == "tool-input-available"
    assert serialized[3]["toolCallId"] == "call_1"
    assert serialized[3]["toolName"] == "shell"
    assert serialized[3]["input"] == {"command": "pwd"}
    assert serialized[4]["type"] == "tool-output-available"
    assert serialized[4]["toolCallId"] == "call_1"
    assert serialized[4]["output"] == {"stdout": "/tmp/alice"}
    assert serialized[5] == {"type": "text-start", "id": "turn_1"}
    assert serialized[6] == {"type": "text-delta", "id": "turn_1", "delta": "done"}
    assert serialized[7] == {"type": "text-end", "id": "turn_1"}
    assert serialized[8] == {"type": "finish"}
