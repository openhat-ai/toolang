"""Execution-local input estimates, calibrated against inclusive provider usage."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math

import tiktoken
from typing import cast

from toolang.base.types.message import Message
from toolang.base.types.run import ModelCall
from toolang.base.types.model import Model


def text_tokens(text: str) -> int:
    return len(tiktoken.get_encoding("o200k_base").encode_ordinary(text))


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


# Provisional corrections from measured reducer requests, not native tokenizers.
# Unmeasured models use the common encoding and learn a local usage correction.
_MODEL_TOKEN_SCALES = {
    "vercel/openai/gpt-6-luna-fast": 0.92,
    "vercel/anthropic/claude-sonnet-5": 1.55,
    "deepseek/deepseek-flash": 1.0,
}


def input_tokens(request: ModelCall, overhead: int = 0) -> int:
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


@dataclass
class TokenCounter:
    """One model's estimates; calibration never crosses a model or run boundary."""

    model_ref: str = ""
    scale: float = 1.0

    @property
    def correction(self) -> float:
        return _MODEL_TOKEN_SCALES.get(self.model_ref, 1.0)

    def base(self, request: ModelCall, overhead: int = 0) -> int:
        return math.ceil(input_tokens(request, overhead) * self.correction)

    def adjust(self, raw: int) -> int:
        return math.ceil(math.ceil(raw * self.correction) * self.scale)

    def message(self, message: Message) -> int:
        return self.adjust(message_tokens(message))

    def calibrated_scale(self, estimate: int, usage: int | None) -> float:
        return (
            max(self.scale, usage / estimate) if estimate > 0 and usage else self.scale
        )

    def observe(self, estimate: int, usage: int | None) -> None:
        self.scale = self.calibrated_scale(estimate, usage)


@dataclass
class InputEstimate:
    counter: TokenCounter = field(default_factory=TokenCounter)
    model: Model | None = None
    raw_tokens: int | None = None
    request: ModelCall | None = None
    binding: object = None
    tokens: int | None = None
    measured: bool = False
    overhead: int = 0

    def bind_model(self, model: Model) -> None:
        if self.model == model:
            return
        self.model = model
        self.counter = TokenCounter(model.ref)
        self.request, self.binding, self.tokens, self.raw_tokens = (
            None,
            None,
            None,
            None,
        )
        self.measured = False
        self.overhead = 0

    def _added(self, request: ModelCall) -> int:
        assert self.request is not None
        return sum(
            message_tokens(m) for m in request.messages[len(self.request.messages) :]
        ) + _added_tokens(self.request.continuation, request.continuation)

    def _raw_count(self, request: ModelCall, binding: object, overhead: int) -> int:
        if self.raw_tokens is not None and self._extends(request, binding, overhead):
            return self.raw_tokens + self._added(request)
        return input_tokens(request, overhead)

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
            return self.tokens + self.counter.adjust(self._added(request))
        return self.counter.adjust(self._raw_count(request, binding, overhead))

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
        raw = self._raw_count(request, binding, overhead)
        self.counter.observe(math.ceil(raw * self.counter.correction), usage)
        measured = usage is not None and usage > 0
        tokens = usage if measured else self.count(request, binding, overhead)
        self.raw_tokens = raw
        self.measured = measured
        self.overhead = overhead
        self.request, self.binding, self.tokens = request, binding, tokens
