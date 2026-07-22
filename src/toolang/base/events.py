"""Shared plugin-facing event types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .types.message import Delta, Part, PartType


@dataclass(frozen=True, slots=True)
class ModelPartStartEvent:
    """One streamed model-part start event."""

    kind: PartType


@dataclass(frozen=True, slots=True)
class ModelPartDeltaEvent:
    """One streamed model-part delta event."""

    delta: Delta


@dataclass(frozen=True, slots=True)
class ModelPartEndEvent:
    """One streamed model-part end event."""

    data: Part


ModelPartEvent = ModelPartStartEvent | ModelPartDeltaEvent | ModelPartEndEvent
ModelEventHandler = Callable[[ModelPartEvent], None]
