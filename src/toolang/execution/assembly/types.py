"""Prepared content shared by model-call assembly and the agic frame."""

from dataclasses import dataclass

from toolang.base.types.message import Message

from ..records import RecallControlPayload


@dataclass(frozen=True, slots=True)
class PreparedPrompt:
    """Rendered once per frame; live messages and tool policy remain per-call."""

    instructions: str
    context: str
    messages: tuple[Message, ...]
    declarations: tuple[RecallControlPayload, ...] = ()
