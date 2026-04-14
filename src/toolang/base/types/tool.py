"""Shared tool value types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One model-facing tool definition."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Resolved context passed into one tool call."""

    run_id: str
    home: Path
    room: Path
    wd: Path
