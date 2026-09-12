"""Prepared content shared by model-call assembly and the agic frame."""

from dataclasses import dataclass

from toolang.base.types.message import Message


@dataclass(frozen=True, slots=True)
class PreparedPrompt:
    """Rendered once per frame; live messages and tool policy remain per-call."""

    instructions: str
    instructions_with_tools: str
    context: str
    messages: tuple[Message, ...]
