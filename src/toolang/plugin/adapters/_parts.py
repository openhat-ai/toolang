"""Part ownership and indexed streaming shared by model adapters."""

from collections.abc import Hashable
from dataclasses import dataclass, field, replace

from toolang.base.errors import ToolangError
from toolang.base.types.message import (
    Delta,
    Message,
    Part,
    ReasoningDelta,
    ReasoningPart,
    TextDelta,
    TextPart,
    ToolCallPart,
)
from toolang.base.types.model import Model
from toolang.base.types.run import (
    ModelPartDelta,
    ModelPartEnd,
    ModelPartStart,
    ModelStreamHandler,
)


def native_metadata(model: Model, adapter: str, **fields: object) -> dict[str, object]:
    return {"adapter": adapter, "model": model.id, **fields}


def compatible(
    part: ReasoningPart | TextPart | ToolCallPart, model: Model, adapter: str
) -> bool:
    return (
        part.provider == model._toolang.provider
        and part.provider_metadata.get("adapter") == adapter
        and part.provider_metadata.get("model") == model.id
    )


@dataclass
class PartStream:
    """Keep canonical order independent of native IDs and chunk positions."""

    on_event: ModelStreamHandler
    parts: list[Part] = field(default_factory=list)
    indices: dict[Hashable, int] = field(default_factory=dict)
    ended: set[int] = field(default_factory=set)
    pending: dict[Hashable, Part] = field(default_factory=dict)

    async def start(self, key: Hashable, part: Part) -> int:
        if key in self.indices:
            index = self.indices[key]
            if self.parts[index].type != part.type:
                raise ToolangError("model stream changed a Part's type")
            return index
        index = len(self.parts)
        self.indices[key] = index
        self.parts.append(part)
        await self.on_event(ModelPartStart(part=index, kind=part.type))
        return index

    async def delta(self, key: Hashable, delta: Delta) -> None:
        index = self.indices[key]
        part = self.parts[index]
        if index in self.ended or part.type != delta.kind:
            raise ToolangError("model delta does not target an open matching Part")
        if isinstance(part, TextPart | ReasoningPart):
            self.parts[index] = replace(part, text=part.text + delta.text)
        if delta.text:
            await self.on_event(ModelPartDelta(part=index, delta=delta))

    async def text(self, key: Hashable, text: str, *, reasoning: bool = False) -> None:
        await self.start(key, ReasoningPart("") if reasoning else TextPart(""))
        await self.delta(key, ReasoningDelta(text) if reasoning else TextDelta(text))

    async def finish(self, key: Hashable, part: Part) -> None:
        index = self.indices.get(key)
        if index in self.ended:
            assert index is not None
            if self.parts[index] != part:
                raise ToolangError("model final snapshot contradicts a completed Part")
            return
        suffix = ""
        if index is not None:
            previous = self.parts[index]
            if previous.type != part.type:
                raise ToolangError("model stream changed a Part's type")
            if isinstance(part, TextPart | ReasoningPart):
                assert isinstance(previous, TextPart | ReasoningPart)
                if not part.text.startswith(previous.text):
                    raise ToolangError("model final snapshot contradicts streamed text")
                suffix = part.text[len(previous.text) :]
        # The native unit is complete before observers see its start or suffix.
        # Keep it available if an observer cancels before the end notification.
        self.pending[key] = part
        index = await self.start(key, part)
        if suffix:
            await self.delta(
                key,
                ReasoningDelta(suffix)
                if isinstance(part, ReasoningPart)
                else TextDelta(suffix),
            )
        self.parts[index] = part
        self.ended.add(index)
        del self.pending[key]
        await self.on_event(ModelPartEnd(part=index, data=part))

    async def interrupt(self) -> None:
        """Close readable prefixes without retaining incomplete native state."""

        for key, index in tuple(self.indices.items()):
            part = self.parts[index]
            if key in self.pending:
                await self.finish(key, self.pending[key])
            elif index not in self.ended and isinstance(part, TextPart | ReasoningPart):
                await self.finish(
                    key,
                    replace(part, signature=None, provider=None, provider_metadata={}),
                )

    def message(self) -> Message:
        return Message(role="assistant", parts=tuple(self.parts))
