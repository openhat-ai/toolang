"""External execution requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from toolang.base.types.message import Message

ExecutableKind = Literal["agic", "flow"]


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One external request to execute an agic or flow."""

    group: str
    origin: str
    run_id: str | None = None
    executable_kind: ExecutableKind = "agic"
    executable_name: str | None = None
    input: str = ""
    message: Message | None = None
    thread_id: str | None = None
    thread_kind: str | None = None
    model_selector: str | None = None
    model_selectors: tuple[str, ...] = ()
    tool_selectors: tuple[str, ...] | None = None
    cap_selectors: tuple[str, ...] = ()
    run_loop: str = "basic"
    delay_sec: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
