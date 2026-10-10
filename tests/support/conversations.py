"""Persisted conversation fixtures with complete protocol metadata."""

from toolang.teaming.schemas import Conversation


def conversation_record(id, kind, participants, name=None, *, revision=1):
    return Conversation(
        id,
        kind,
        participants,
        name,
        created_by="human:bryan",
        created_at="2026-10-10T00:00:00.000Z",
        updated_at="2026-10-10T00:00:00.000Z",
        revision=revision,
    )
