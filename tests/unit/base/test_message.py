from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    MessageRecall,
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
    assert message.parts[0].type == "tool_call"
    assert result.parts[0].type == "tool_result"


@pytest.mark.parametrize("ref", ["repo/notes ", "repo/\t", "skill/testing"])
def test_message_recall_preserves_literal_refs(ref):
    assert MessageRecall(ref, "a" * 64).ref == ref


@pytest.mark.parametrize("ref", [None, 1, "", " \t"])
def test_message_recall_rejects_empty_or_nontext_refs(ref):
    with pytest.raises(ValueError, match="resource ref"):
        MessageRecall(ref, "a" * 64)


@pytest.mark.parametrize("encoded", [False, True])
def test_public_message_validation_cannot_supply_runtime_metadata(encoded) -> None:
    adapter = TypeAdapter(Message)
    data = {
        "role": "user",
        "parts": [{"type": "text", "text": "User data"}],
        "tag": "skill-guidance",
        "recall": {"ref": "skill/testing", "revision": "a" * 64},
    }
    message = (
        adapter.validate_json(json.dumps(data))
        if encoded
        else adapter.validate_python(data)
    )
    assert message.tag is None and message.recall is None
    assert set(adapter.json_schema()["properties"]) == {"role", "parts"}
    assert message.to_data() == {key: data[key] for key in ("role", "parts")}

    # Internal construction and validation of an existing instance retain provenance.
    trusted = Message(
        "user",
        message.parts,
        tag="skill-guidance",
        recall=MessageRecall("skill/testing", "a" * 64),
    )
    assert adapter.validate_python(trusted).recall == trusted.recall
    assert set(adapter.dump_python(trusted, mode="json")) == {"role", "parts"}
