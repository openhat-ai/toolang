"""Execution-local input estimates, calibrated against inclusive provider usage."""

from dataclasses import dataclass
import json

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


@dataclass
class InputEstimate:
    request: ModelCall | None = None
    binding: object = None
    tokens: int | None = None

    def count(self, request: ModelCall, binding: object) -> int:
        previous = self.request
        if (
            previous is not None
            and self.tokens is not None
            and binding == self.binding
            and request.instructions == previous.instructions
            and request.tools == previous.tools
            and request.output_schema == previous.output_schema
            and request.messages[: len(previous.messages)] == previous.messages
        ):
            return self.tokens + sum(
                message_tokens(m) for m in request.messages[len(previous.messages) :]
            )
        fixed = json.dumps(
            {
                "instructions": request.instructions,
                "tools": [tool.to_data() for tool in request.tools],
                "output_schema": request.output_schema,
            },
            ensure_ascii=False,
        )
        return (
            32 + text_tokens(fixed) + sum(message_tokens(m) for m in request.messages)
        )

    def observe(self, request: ModelCall, binding: object, usage: int | None) -> None:
        tokens = (
            usage if usage is not None and usage > 0 else self.count(request, binding)
        )
        self.request, self.binding, self.tokens = request, binding, tokens
