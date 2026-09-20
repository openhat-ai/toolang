"""Execution-local input estimates, calibrated against inclusive provider usage."""

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import cast

from toolang.base.types.message import Message
from toolang.base.types.run import ModelCall


def text_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 2) // 3


def message_tokens(message: Message) -> int:
    # References do not reveal media dimensions/duration. Reserve 4096 per media
    # part in addition to the serialized payload; provider usage calibrates later.
    data = message.to_data()
    media = sum(part.type in {"image", "audio", "document"} for part in message.parts)
    return 8 + text_tokens(json.dumps(data, ensure_ascii=False)) + media * 4096


def _added_tokens(previous: object, current: object) -> int:
    """Conservatively count changed continuation content without discounting usage."""
    if previous == current or current is None:
        return 0
    if isinstance(previous, Mapping) and isinstance(current, Mapping):
        before = cast(Mapping[str, object], previous)
        after = cast(Mapping[str, object], current)
        return sum(
            _added_tokens(before[key], value)
            if key in before
            else text_tokens(json.dumps({key: value}, ensure_ascii=False))
            for key, value in after.items()
        )
    return text_tokens(json.dumps(current, ensure_ascii=False))


@dataclass
class InputEstimate:
    request: ModelCall | None = None
    binding: object = None
    tokens: int | None = None
    measured: bool = False
    overhead: int = 0

    def _extends(self, request: ModelCall, binding: object, overhead: int) -> bool:
        previous = self.request
        return (
            previous is not None
            and self.tokens is not None
            and binding == self.binding
            and overhead == self.overhead
            and request.instructions == previous.instructions
            and request.tools == previous.tools
            and request.reasoning == previous.reasoning
            and request.output_schema == previous.output_schema
            and request.messages[: len(previous.messages)] == previous.messages
        )

    def count(self, request: ModelCall, binding: object, overhead: int = 0) -> int:
        previous = self.request
        if (
            previous is not None
            and self.tokens is not None
            and self._extends(request, binding, overhead)
        ):
            return (
                self.tokens
                + sum(
                    message_tokens(m)
                    for m in request.messages[len(previous.messages) :]
                )
                + _added_tokens(previous.continuation, request.continuation)
            )
        fixed = json.dumps(
            {
                "instructions": request.instructions,
                "tools": [tool.to_data() for tool in request.tools],
                "output_schema": request.output_schema,
                "continuation": request.continuation,
                "reasoning": request.reasoning.to_data() if request.reasoning else None,
            },
            ensure_ascii=False,
        )
        # A schema may also be embedded in adapter-added instructions, or expanded
        # from a root $ref for native output. Reserve a second copy and directive
        # framing, in addition to the per-message and global wire overhead.
        schema_overhead = (
            256 + text_tokens(json.dumps(request.output_schema, ensure_ascii=False))
            if request.output_schema is not None
            else 0
        )
        return (
            32
            + text_tokens(fixed)
            + schema_overhead
            + overhead
            + sum(message_tokens(m) for m in request.messages)
        )

    def reliable_count(
        self, request: ModelCall, binding: object, overhead: int = 0
    ) -> int | None:
        """Return the calibrated count, or None before provider usage is known."""

        if not self.measured or not self._extends(request, binding, overhead):
            return None
        return self.count(request, binding, overhead)

    def observe(
        self, request: ModelCall, binding: object, usage: int | None, overhead: int = 0
    ) -> None:
        measured = usage is not None and usage > 0
        tokens = usage if measured else self.count(request, binding, overhead)
        self.measured = measured
        self.overhead = overhead
        self.request, self.binding, self.tokens = request, binding, tokens
