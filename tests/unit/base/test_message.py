from __future__ import annotations

import pytest

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)


def test_document_part_round_trips_canonical_data() -> None:
    message = Message.from_data(
        {
            "role": "user",
            "parts": [
                {
                    "type": "document",
                    "data": "data:application/pdf;base64,JVBERi0xLjc=",
                    "filename": "report.pdf",
                }
            ],
        }
    )

    assert message == Message(
        role="user",
        parts=(
            DocumentPart(
                data="data:application/pdf;base64,JVBERi0xLjc=",
                filename="report.pdf",
                media_type="application/pdf",
            ),
        ),
    )
    assert message.to_data() == {
        "role": "user",
        "parts": [
            {
                "type": "document",
                "data": "data:application/pdf;base64,JVBERi0xLjc=",
                "filename": "report.pdf",
                "media_type": "application/pdf",
            }
        ],
    }


def test_audio_transcript_belongs_to_audio_part() -> None:
    message = Message(
        role="assistant",
        parts=(
            AudioPart(
                data="ZGF0YQ==",
                format="mp3",
                transcript="hello",
            ),
        ),
    )

    assert message.to_data() == {
        "role": "assistant",
        "parts": [
            {
                "type": "audio",
                "data": "ZGF0YQ==",
                "format": "mp3",
                "transcript": "hello",
            }
        ],
    }


def test_message_validates_role_part_combinations() -> None:
    with pytest.raises(ValueError, match="user messages"):
        Message(
            role="user",
            parts=(
                ToolCallPart(
                    tool_call_id="call-1",
                    tool_name="lookup",
                    tool_family="lookup",
                ),
            ),
        )

    with pytest.raises(ValueError, match="tool messages require"):
        Message(role="tool")

    Message(
        role="user",
        parts=(
            TextPart("describe"),
            ImagePart(image_url="https://example.com/image.png"),
            AudioPart(data="ZGF0YQ==", format="wav"),
            DocumentPart(file_id="file-1"),
        ),
    )

    with pytest.raises(ValueError, match="exactly one"):
        ImagePart(
            image_url="https://example.com/image.png",
            file_id="image-1",
        )

    with pytest.raises(ValueError, match="exactly one"):
        DocumentPart(
            data="ZGF0YQ==",
            file_id="file-1",
        )


def test_tool_part_metadata_round_trips_without_message_meta() -> None:
    message = Message(
        role="assistant",
        parts=(
            ToolCallPart(
                tool_call_id="call-1",
                tool_name="lookup",
                tool_family="lookup",
                reasoning="Need current data.",
            ),
        ),
    )
    result = Message(
        role="tool",
        parts=(
            ToolResultPart(
                tool_call_id="call-1",
                tool_name="lookup",
                tool_family="lookup",
                error="unavailable",
            ),
        ),
    )

    assert Message.from_data(message.to_data()) == message
    assert Message.from_data(result.to_data()) == result
    with pytest.raises(ValueError, match="not a Percept"):
        _ = message.percept
