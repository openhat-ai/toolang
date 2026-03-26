"""Ingress request shapes for the runtime host and scheduler."""

from __future__ import annotations

from dataclasses import dataclass

from toolang.concepts.execution import Message, MessageOrigin

RunSubmissionKind = MessageOrigin


@dataclass(frozen=True, slots=True)
class RunSubmission:
    """One normalized run submission entering the runtime scheduler."""

    kind: RunSubmissionKind
    thread_id: str | None
    message: Message | None = None
