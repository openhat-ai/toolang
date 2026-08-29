"""CLI rendering for operational progress events."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from threading import RLock, Timer
import time
from typing import Callable, TextIO

from rich.console import Console
from rich.live import Live
from rich.text import Text

from ...common.events import ProgressEvent
from ...common.progress import ProgressSink
from .execution_progress.config import resolve_progress_max_width
from .execution_progress.formatting import one_line, wrap_display


_LIVE_REVEAL_SECONDS = 0.15
_ELAPSED_REVEAL_SECONDS = 1.0
_MAX_DETAIL = 80


@dataclass(slots=True)
class _ItemState:
    """Private presentation state for one opaque progress item."""

    event: ProgressEvent
    order: int
    activity_started_at: float | None = None
    activity_elapsed: float | None = None
    began: bool = False

    @property
    def active(self) -> bool:
        return self.event.status == "running" and self.event.label.endswith("...")


class CliProgress:
    """Present one contiguous operational segment on stderr."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        enabled: bool = True,
        max_width: int | None = None,
        leading_gap: bool = False,
        _clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stream = stream or sys.stderr
        self._enabled = enabled
        self._terminal = bool(getattr(self._stream, "isatty", lambda: False)())
        self._max_width = (
            resolve_progress_max_width(os.environ) if max_width is None else max_width
        )
        if self._max_width < 1:
            raise ValueError("progress max width must be positive")
        self._clock = _clock
        self._leading_gap = leading_gap
        self._console = Console(
            file=self._stream,
            force_terminal=True if self._terminal else False,
            color_system="auto" if self._terminal else None,
            highlight=False,
            width=self._available_width(),
        )
        self._items: dict[tuple[str, str], _ItemState] = {}
        self._order = 0
        self._selected_terminal: tuple[str, str] | None = None
        self._failure_event: ProgressEvent | None = None
        self._plain_events: set[
            tuple[str, str, str, str, str, str | None, str | None]
        ] = set()
        self._plain_started = False
        self._prepare_registered: set[str] = set()
        self._prepare_completed: set[str] = set()
        self._live_display: Live | None = None
        self._reveal_timer: Timer | None = None
        self._refresh_timer: Timer | None = None
        self._closed = False
        self._lock = RLock()

    def __enter__(self) -> CliProgress:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            if self._closed:
                return
            previous = self._items.get((event.kind, event.id))
            if _same_event(previous.event if previous is not None else None, event):
                return
            self._record(event, previous)
            if not self._enabled or event.status == "pending":
                return
            if event.status == "failed":
                self._clear_live()
                return
            if self._terminal:
                self._render_terminal()
            else:
                self._render_plain(event, previous)

    @property
    def sink(self) -> ProgressSink | None:
        """Return this segment's sink, or ``None`` when presentation is disabled."""

        return self if self._enabled else None

    @property
    def current_stage(self) -> str | None:
        with self._lock:
            state = self._selected_state()
            return state.event.label if state is not None else None

    @property
    def failure_reason(self) -> str | None:
        with self._lock:
            return (
                _bounded_detail(self._failure_event.detail)
                if self._failure_event is not None
                else None
            )

    @property
    def failure_stage(self) -> str | None:
        with self._lock:
            event = self._failure_event
            return f"{event.kind}.{event.stage}" if event is not None else None

    @property
    def failure_label(self) -> str | None:
        with self._lock:
            return (
                self._failure_event.label if self._failure_event is not None else None
            )

    def close(self) -> None:
        """Close this segment idempotently without retaining successful output."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancel_reveal()
            self._clear_live()

    def failure_message(
        self,
        error: BaseException,
        *,
        reason: str | None = None,
        fix: str | None = None,
        log_path: Path | None = None,
    ) -> str:
        """Compose the single stable block for this segment's first failure."""

        with self._lock:
            event = self._failure_event
            if event is None:
                return str(error).strip() or type(error).__name__
            fallback_reason = str(error).strip() or type(error).__name__
            lines = [
                event.label,
                f"  Stage: {event.kind}.{event.stage}",
                f"  Reason: {reason or _bounded_detail(event.detail) or fallback_reason}",
            ]
            if fix is not None:
                lines.append(f"  Fix: {fix}")
            if log_path is not None:
                lines.append(f"  Log: {log_path}")
            return "\n".join(lines)

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Temporarily release terminal ownership for committed output."""

        with self._lock:
            was_visible = self._live_display is not None
            was_pending = self._reveal_timer is not None
            self._cancel_reveal()
            self._clear_live()
        try:
            yield
        finally:
            with self._lock:
                if self._closed or self._selected_state() is None:
                    return
                if was_visible:
                    self._reveal_live()
                elif was_pending:
                    self._render_terminal()

    def _record(self, event: ProgressEvent, previous: _ItemState | None) -> None:
        now = self._clock()
        self._order += 1
        if event.kind == "prepare" and event.status == "pending":
            self._register_prepare(event.id)

        same_stage = previous is not None and previous.event.stage == event.stage
        began = previous.began if previous is not None and same_stage else False
        started_at = previous.activity_started_at if previous is not None else None
        elapsed = previous.activity_elapsed if previous is not None else None
        if event.status == "running":
            if event.label.endswith("..."):
                begins_activity = (
                    previous is None
                    or not previous.active
                    or previous.event.stage != event.stage
                    or previous.event.label != event.label
                )
                if begins_activity:
                    started_at = now
                    elapsed = None
                began = True
            else:
                if same_stage and previous is not None and previous.active:
                    elapsed = (
                        max(now - previous.activity_started_at, 0.0)
                        if previous.activity_started_at is not None
                        else None
                    )
                started_at = None
        elif event.status in {"ok", "skipped", "failed"}:
            if same_stage and previous is not None and previous.active:
                started_at = previous.activity_started_at
                elapsed = max(now - started_at, 0.0) if started_at is not None else None

        key = (event.kind, event.id)
        self._items[key] = _ItemState(
            event=event,
            order=self._order,
            activity_started_at=started_at,
            activity_elapsed=elapsed,
            began=began,
        )
        if (
            event.kind == "prepare"
            and event.id in self._prepare_registered
            and event.stage == "materialize"
            and event.status in {"ok", "skipped"}
        ):
            self._prepare_completed.add(event.id)
        if event.status == "failed" and self._failure_event is None:
            self._failure_event = event
        if (
            event.status in {"ok", "skipped"}
            or (event.status == "running" and not event.label.endswith("..."))
        ) and began:
            self._selected_terminal = key
        elif event.status == "running" and event.label.endswith("..."):
            self._selected_terminal = None

    def _register_prepare(self, item_id: str) -> None:
        if (
            self._prepare_registered
            and self._prepare_registered <= self._prepare_completed
        ):
            self._prepare_registered.clear()
            self._prepare_completed.clear()
        self._prepare_registered.add(item_id)

    def _render_plain(self, event: ProgressEvent, previous: _ItemState | None) -> None:
        if event.status in {"ok", "skipped"} and not (
            previous is not None
            and previous.event.stage == event.stage
            and previous.began
        ):
            return
        if event.status not in {"running", "ok", "skipped"}:
            return
        key = (
            event.kind,
            event.id,
            event.stage,
            event.status,
            event.label,
            _bounded_detail(event.detail),
            self._prepare_fact(event),
        )
        if key in self._plain_events:
            return
        self._plain_events.add(key)
        if self._leading_gap and not self._plain_started:
            print(file=self._stream)
        self._plain_started = True
        for line in self._wrapped_text(self._event_text(event, terminal=False)):
            print(line, file=self._stream)

    def _render_terminal(self) -> None:
        if self._selected_state() is None:
            self._cancel_reveal()
            self._clear_live()
            return
        if self._live_display is not None:
            self._live_display.update(self._live_text(), refresh=True)
            self._schedule_refresh()
            return
        if self._reveal_timer is None:
            timer = Timer(_LIVE_REVEAL_SECONDS, self._reveal_live)
            timer.daemon = True
            self._reveal_timer = timer
            timer.start()

    def _reveal_live(self) -> None:
        with self._lock:
            self._reveal_timer = None
            if self._closed or self._selected_state() is None:
                return
            self._live_display = Live(
                self._live_text(),
                console=self._console,
                auto_refresh=False,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self._live_display.start(refresh=True)
            self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        self._cancel_refresh()
        state = self._selected_state()
        if state is None or not state.active or state.activity_started_at is None:
            return
        elapsed = max(self._clock() - state.activity_started_at, 0.0)
        interval = 0.1 if elapsed < 10 else 1.0
        timer = Timer(interval, self._refresh_elapsed)
        timer.daemon = True
        self._refresh_timer = timer
        timer.start()

    def _refresh_elapsed(self) -> None:
        with self._lock:
            self._refresh_timer = None
            if self._closed or self._live_display is None:
                return
            self._live_display.update(self._live_text(), refresh=True)
            self._schedule_refresh()

    def _cancel_reveal(self) -> None:
        timer = self._reveal_timer
        self._reveal_timer = None
        if timer is not None:
            timer.cancel()

    def _clear_live(self) -> None:
        self._cancel_refresh()
        display = self._live_display
        self._live_display = None
        if display is not None:
            display.stop()

    def _cancel_refresh(self) -> None:
        timer = self._refresh_timer
        self._refresh_timer = None
        if timer is not None:
            timer.cancel()

    def _selected_state(self) -> _ItemState | None:
        active = [state for state in self._items.values() if state.active]
        if active:
            return max(active, key=lambda state: state.order)
        if self._selected_terminal is None:
            return None
        return self._items.get(self._selected_terminal)

    def _live_text(self) -> Text:
        state = self._selected_state()
        if state is None:
            return Text()
        value = self._state_text(state)
        return Text("\n".join(self._wrapped_text(value)), style="dim")

    def _state_text(self, state: _ItemState) -> str:
        elapsed: str | None = None
        if state.active and state.activity_started_at is not None:
            seconds = max(self._clock() - state.activity_started_at, 0.0)
            if seconds >= _ELAPSED_REVEAL_SECONDS:
                elapsed = _format_elapsed(seconds)
        elif (
            not state.active
            and state.activity_elapsed is not None
            and state.activity_elapsed >= _ELAPSED_REVEAL_SECONDS
        ):
            elapsed = _format_elapsed(state.activity_elapsed)
        return self._event_text(state.event, terminal=True, elapsed=elapsed)

    def _event_text(
        self,
        event: ProgressEvent,
        *,
        terminal: bool,
        elapsed: str | None = None,
    ) -> str:
        del terminal
        facts = [value for value in (self._prepare_fact(event), elapsed) if value]
        return _with_facts(one_line(event.label), facts)

    def _prepare_fact(self, event: ProgressEvent) -> str | None:
        if event.kind != "prepare" or event.id not in self._prepare_registered:
            return None
        total = len(self._prepare_registered)
        if total <= 1:
            return None
        return f"{len(self._prepare_completed)}/{total} caps"

    def _wrapped_text(self, value: str) -> tuple[str, ...]:
        width = self._available_width()
        if width <= 2:
            return tuple(wrap_display(value, width))
        lines: list[str] = []
        remaining = value
        available = width
        while remaining:
            line = wrap_display(remaining, available)[0]
            prefix = "" if not lines else "  "
            lines.append(f"{prefix}{line}")
            remaining = remaining[len(line) :].lstrip()
            available = width - 2
        return tuple(lines) or ("",)

    def _available_width(self) -> int:
        if not self._terminal:
            return self._max_width
        try:
            terminal_width = os.get_terminal_size(self._stream.fileno()).columns
        except (AttributeError, OSError, ValueError):
            terminal_width = self._max_width
        return max(1, min(terminal_width, self._max_width))


def make_cli_progress(
    *,
    stream: TextIO | None = None,
    enabled: bool = True,
    max_width: int | None = None,
    leading_gap: bool = False,
) -> CliProgress:
    """Create one invocation-scoped operational presenter."""

    return CliProgress(
        stream=stream,
        enabled=enabled,
        max_width=max_width,
        leading_gap=leading_gap,
    )


def _same_event(previous: ProgressEvent | None, current: ProgressEvent) -> bool:
    return previous == current


def _with_facts(label: str, facts: list[str]) -> str:
    if not facts:
        return label
    running = label.endswith("...")
    base = label[:-3].rstrip() if running else label
    suffix = f" ({', '.join(facts)})"
    return f"{base}{suffix}{'...' if running else ''}"


def _format_elapsed(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f}s"
    rounded = round(seconds)
    if rounded < 60:
        return f"{rounded}s"
    minutes, secs = divmod(rounded, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s"


def _bounded_detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    normalized = one_line(detail)
    if not normalized:
        return None
    if len(normalized) <= _MAX_DETAIL:
        return normalized
    return normalized[: _MAX_DETAIL - 1] + "…"


def runtime_startup_failure_message(
    progress: CliProgress,
    error: BaseException,
    *,
    log_path: Path | None = None,
    dev_artifact: Path | None = None,
    development_build: bool = False,
) -> str:
    """Describe one failed AgentServer startup without duplicating its cause."""

    fallback_reason = (
        progress.failure_reason or str(error).strip() or type(error).__name__
    )
    reason, fix = _runtime_failure_guidance(
        progress.failure_label,
        fallback_reason=fallback_reason,
        dev_artifact=dev_artifact,
        development_build=development_build,
    )
    return progress.failure_message(
        error,
        reason=reason,
        fix=fix,
        log_path=log_path,
    )


def _runtime_failure_guidance(
    activity: str | None,
    *,
    fallback_reason: str,
    dev_artifact: Path | None,
    development_build: bool,
) -> tuple[str, str | None]:
    if activity and "install Toolang" in activity:
        if dev_artifact is not None:
            return (
                f"Could not install Toolang from {dev_artifact.name}",
                "Rebuild the wheel and check the installation log",
            )
        return (
            "Could not install Toolang from the package index",
            "Check network access or use --dev PATH with a compatible wheel",
        )
    if activity and "check Toolang" in activity:
        if dev_artifact is not None:
            return (
                "The selected Toolang wheel cannot start the required AgentServer",
                "Rebuild or select a compatible Toolang wheel, then use --dev PATH",
            )
        if development_build:
            return (
                "The guest Toolang package cannot start the required AgentServer",
                "Build the current source with `uv build --wheel`, then use --dev dist",
            )
        return (
            "The guest Toolang package cannot start the required AgentServer",
            "Build or select a compatible Toolang wheel, then use --dev PATH",
        )
    return fallback_reason, None
