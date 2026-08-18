"""Shared terminal chat interfaces and value types."""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Any, Protocol, cast

from toolang.base.types.message import Part
from toolang.execution.events import RunEvent
from toolang.execution.records import execution_error_message
from toolang.execution.types import ExecutionError, RunOverride

if TYPE_CHECKING:
    from .blocks import MutableBlock
    from .presenter import ChatRunPresenter


@dataclass(frozen=True, slots=True)
class ChatResult:
    """One durable run result requested by the chat presentation."""

    run_id: str
    output: tuple[Part, ...]


@dataclass(frozen=True, slots=True)
class QueuedCall:
    """One chat call with settings captured when it was submitted."""

    source: str
    selects: Mapping[str, object]


class ChatClient(Protocol):
    def list_models(self) -> Mapping[str, Any]: ...

    def list_executables(self, kind: str) -> Mapping[str, Any]: ...

    def create_thread(self) -> str: ...

    def apply_settings(
        self,
        commands: tuple[RunOverride, ...],
        selects: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def get_result(
        self,
        run_id: str | None,
        *,
        thread_id: str | None,
    ) -> ChatResult: ...

    def start_run(
        self,
        thread_id: str,
        message: str,
        selects: Mapping[str, object],
        on_event: Callable[[RunEvent], None],
        on_error: Callable[[str], None],
    ) -> None: ...

    def stop_run(
        self,
        run_id: str,
        on_error: Callable[[str], None],
    ) -> None: ...

    def steer_run(
        self,
        run_id: str,
        message: str,
        on_error: Callable[[str], None],
    ) -> None: ...


class AppContext(Protocol):
    def get_selects(self) -> dict[str, object]: ...

    def get_client(self) -> ChatClient: ...

    def get_queue(self) -> list[QueuedCall]: ...

    def get_active_run(self) -> str | None: ...

    def get_thread_id(self) -> str | None: ...

    def set_active_run(self, run_id: str | None) -> None: ...

    def get_live_blocks(self) -> list["MutableBlock"]: ...

    def get_presenter(self) -> "ChatRunPresenter": ...

    def ensure_thread_id(self) -> str: ...

    def is_busy(self) -> bool: ...

    def finalize_block(self, block: "MutableBlock") -> None: ...

    def finish_run(self) -> None: ...

    def set_status_error(self, message: str) -> None: ...

    def refresh_status(self) -> None: ...

    def replace_input(self, text: str) -> None: ...

    def request_steer(self, message: str) -> None: ...

    def request_exit(self) -> None: ...


def chat_status_label(selects: Mapping[str, object]) -> str:
    model = as_text(selects.get("model"))
    model_label = model or "default"
    flow = as_text(selects.get("flow"))
    agic = as_text(selects.get("agic"))
    runnable = as_text(selects.get("runnable"))
    if agic == "default":
        agic = None
    executable = (
        f"flow:{flow}"
        if flow
        else f"agic:{agic}"
        if agic
        else f"runnable:{runnable}"
        if runnable
        else ""
    )
    return f"{model_label}  {executable}" if executable else model_label


def friendly_error(message: ExecutionError) -> str:
    text = (execution_error_message(message) or "").strip()
    extracted = _extract_error_message(text)
    if extracted:
        return extracted
    return text


def _extract_error_message(text: str) -> str | None:
    candidates = [text]
    if " - " in text:
        candidates.append(text.split(" - ", 1)[1].strip())
    for candidate in candidates:
        parsed = _parse_error_payload(candidate)
        if parsed is None:
            continue
        error = parsed.get("error")
        if isinstance(error, Mapping):
            message = as_text(error.get("message"))
            if message is not None:
                return message
        message = as_text(parsed.get("message"))
        if message is not None:
            return message
    return None


def _parse_error_payload(text: str) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return None
    return cast(Mapping[str, Any], parsed) if isinstance(parsed, Mapping) else None


def as_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
