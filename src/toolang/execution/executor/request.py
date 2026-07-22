"""External run requests accepted by the executor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from toolang.base.types.message import Message

ExecutableKind = Literal["agic", "flow"]


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One external request to execute an agic or flow."""

    origin: str
    input: Message = field(default_factory=lambda: Message.user(""))
    run_id: str | None = None
    thread_id: str | None = None
    executable_kind: ExecutableKind = "agic"
    executable_name: str | None = None
    model_selector: str | None = None
    request_id: str | None = None
    context: dict[str, object] = field(default_factory=dict)
