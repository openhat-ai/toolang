"""Recall revisions and visibility derived from recorded message references."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
import re

from .records import ControlRecord, RecallControlPayload
from .types import ControlRef, MessageDelta, RecallTarget, TypedRef


def canonical_recall(payload: RecallControlPayload) -> RecallControlPayload:
    """Normalize an incoming revision once, before comparison or persistence."""

    if re.fullmatch(r"[0-9a-fA-F]{1,64}", payload.revision) is None:
        raise ValueError("recall revision must contain 1–64 hexadecimal digits")
    value = int(payload.revision, 16)
    revision = f"{value:064x}" if value else "0"
    if not value and payload.content:
        raise ValueError("a removed recall must have empty content")
    return replace(payload, revision=revision)


def recall_revisions(
    deltas: Iterable[MessageDelta],
    control: Callable[[ControlRef], ControlRecord],
) -> dict[RecallTarget, str]:
    """Find the last recalled revision of each target in actual user templates."""

    revisions: dict[RecallTarget, str] = {}
    for delta in deltas:
        for message in delta.messages:
            if message.role != "user":
                continue
            for segment in message.segments:
                if (
                    isinstance(segment, TypedRef)
                    and isinstance(segment.ref.record, ControlRef)
                    and segment.ref.tokens == ("payload", "content")
                    and segment.type == "Text"
                ):
                    record = control(segment.ref.record)
                    if record.status == "applied" and isinstance(
                        record.payload, RecallControlPayload
                    ):
                        revisions[record.payload.target] = record.payload.revision
    return revisions
