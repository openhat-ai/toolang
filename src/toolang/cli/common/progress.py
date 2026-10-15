"""CLI presentation for operational progress events."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
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
from ...common.time import format_duration
from .execution_progress.config import resolve_progress_max_width
from .execution_progress.formatting import one_line, wrap_display, truncate


_LIVE_REVEAL_SECONDS = 0.15
_ELAPSED_REVEAL_SECONDS = 1.0
_MAX_DETAIL = 80
_PREPARATION_PROGRESS: ContextVar[PreparationProgress | None] = ContextVar(
    "preparation_progress", default=None
)


@dataclass(slots=True)
class _ItemState:
    event: ProgressEvent
    order: int
    began: bool = False
    activity_started_at: float | None = None
    activity_elapsed: float | None = None

    @property
    def active(self) -> bool:
        return self.event.status == "running" and self.event.label.endswith("...")


class CliProgress:
    """Present one uninterrupted operational terminal segment."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        enabled: bool = True,
        max_width: int | None = None,
        leading_gap: bool = False,
        _clock: Callable[[], float] = time.monotonic,
        _reveal_seconds: float = _LIVE_REVEAL_SECONDS,
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
        self._reveal_seconds = _reveal_seconds
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
        self._prepare_registered: set[str] = set()
        self._prepare_completed: set[str] = set()
        self._prepare_terminal: set[str] = set()
        self._plain_started = False
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
            if self._closed or self._failure_event is not None:
                return
            key = (event.kind, event.id)
            previous = self._items.get(key)
            if previous is not None and previous.event == event:
                return
            self._record(key, event, previous)
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
        """Return the invocation sink, or ``None`` when presentation is disabled."""

        return self if self._enabled else None

    @property
    def failure_reason(self) -> str | None:
        with self._lock:
            if self._failure_event is None:
                return None
            return _bounded_detail(self._failure_event.detail)

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
        """Close the segment idempotently without retaining successful TTY output."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._clear_live()

    def complete_preparation(self) -> None:
        """Finish a preparation display, when supported by this presenter."""

    def failure_message(
        self,
        error: BaseException,
        *,
        reason: str | None = None,
        fix: str | None = None,
        log_path: Path | None = None,
    ) -> str:
        """Compose the stable block for the first failed event in this segment."""

        with self._lock:
            event = self._failure_event
            if event is None:
                return str(error).strip() or type(error).__name__
            fallback = str(error).strip() or type(error).__name__
            lines = [
                event.label,
                f"  Stage: {event.kind}.{event.stage}",
                f"  Reason: {reason or _bounded_detail(event.detail) or fallback}",
            ]
            if fix is not None:
                lines.append(f"  Fix: {fix}")
            if log_path is not None:
                lines.append(f"  Log: {log_path}")
            return "\n".join(lines)

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Temporarily release a TTY live region for stable output."""

        with self._lock:
            was_visible = self._live_display is not None
            was_pending = self._reveal_timer is not None
            self._cancel_reveal()
            self._clear_live()
        try:
            yield
        finally:
            with self._lock:
                if not self._closed and self._selected_state() is not None:
                    if was_visible:
                        self._reveal_live()
                    elif was_pending:
                        self._render_terminal()

    def _record(
        self,
        key: tuple[str, str],
        event: ProgressEvent,
        previous: _ItemState | None,
    ) -> None:
        now = self._clock()
        self._order += 1
        if event.kind == "prepare" and event.status == "pending":
            self._register_prepare(event.id)

        same_stage = previous is not None and previous.event.stage == event.stage
        began = previous.began if previous is not None and same_stage else False
        started_at = (
            previous.activity_started_at
            if previous is not None and same_stage
            else None
        )
        elapsed = None

        if event.status == "running":
            if event.label.endswith("..."):
                starts_activity = (
                    previous is None
                    or not previous.active
                    or not same_stage
                    or previous.event.label != event.label
                )
                if starts_activity:
                    started_at = now
                    elapsed = None
                began = True
            else:
                if previous is not None and previous.active and same_stage:
                    elapsed = (
                        max(now - previous.activity_started_at, 0.0)
                        if previous.activity_started_at is not None
                        else None
                    )
                started_at = None
        elif event.status in {"ok", "skipped", "failed"}:
            if previous is not None and previous.active and same_stage:
                started_at = previous.activity_started_at
                elapsed = max(now - started_at, 0.0) if started_at is not None else None

        self._items[key] = _ItemState(
            event=event,
            order=self._order,
            began=began,
            activity_started_at=started_at,
            activity_elapsed=elapsed,
        )
        if (
            event.kind == "prepare"
            and event.id in self._prepare_registered
            and event.stage == "materialize"
            and event.status in {"ok", "skipped", "failed"}
        ):
            self._prepare_terminal.add(event.id)
            if event.status in {"ok", "skipped"}:
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
            and self._prepare_registered <= self._prepare_terminal
        ):
            self._prepare_registered.clear()
            self._prepare_completed.clear()
            self._prepare_terminal.clear()
        self._prepare_registered.add(item_id)

    def _render_plain(
        self,
        event: ProgressEvent,
        previous: _ItemState | None,
    ) -> None:
        if event.status in {"ok", "skipped"} and not (
            previous is not None
            and previous.event.stage == event.stage
            and previous.began
        ):
            return
        if event.status not in {"running", "ok", "skipped"}:
            return
        if self._leading_gap and not self._plain_started:
            print(file=self._stream)
        self._plain_started = True
        for line in self._wrapped_text(self._event_text(event, elapsed=None)):
            print(line, file=self._stream)

    def _render_terminal(self) -> None:
        state = self._selected_state()
        if state is None:
            self._cancel_reveal()
            self._clear_live()
            return
        if self._live_display is not None:
            self._live_display.update(self._live_text(), refresh=True)
            self._schedule_refresh()
            return
        if not state.active:
            self._cancel_reveal()
            return
        if self._reveal_timer is None:
            timer = Timer(self._reveal_seconds, self._reveal_live)
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

    def _cancel_refresh(self) -> None:
        timer = self._refresh_timer
        self._refresh_timer = None
        if timer is not None:
            timer.cancel()

    def _clear_live(self) -> None:
        self._cancel_reveal()
        self._cancel_refresh()
        display = self._live_display
        self._live_display = None
        if display is not None:
            display.stop()

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
        return Text(
            "\n".join(self._wrapped_text(self._state_text(state))),
            style="dim",
        )

    def _state_text(self, state: _ItemState) -> str:
        elapsed: str | None = None
        if state.active and state.activity_started_at is not None:
            seconds = max(self._clock() - state.activity_started_at, 0.0)
            if seconds >= _ELAPSED_REVEAL_SECONDS:
                elapsed = format_duration(seconds)
        elif (
            state.activity_elapsed is not None
            and state.activity_elapsed >= _ELAPSED_REVEAL_SECONDS
        ):
            elapsed = format_duration(state.activity_elapsed)
        return self._event_text(state.event, elapsed=elapsed)

    def _event_text(self, event: ProgressEvent, *, elapsed: str | None) -> str:
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


class PreparationProgress(CliProgress):
    """Show uncached preparation in reusable rows, then one dim summary.

    The CLI owner completes the phase explicitly or forwards a State completion
    event. Closing a scope only releases its display; it never implies success.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._slots: list[tuple[tuple[str | None, str], ProgressEvent] | None] = []
        self._finished: set[tuple[str | None, str]] = set()
        self._caps: set[tuple[str | None, str]] = set()
        self._started: float | None = None
        self._agent_work = False
        self._preparation_done = False
        self._depth = 0
        self._token: Token[PreparationProgress | None] | None = None
        self._last_slot_render = float("-inf")
        self._slot_timer: Timer | None = None
        self._suspended = 0
        self._deferred_output: Text | None = None

    def __enter__(self) -> PreparationProgress:
        if self._depth == 0:
            self._token = _PREPARATION_PROGRESS.set(self)
        self._depth += 1
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            if self._token is not None:
                _PREPARATION_PROGRESS.reset(self._token)
                self._token = None
            super().__exit__(exc_type, exc, traceback)

    @contextmanager
    def suspended(self) -> Iterator[None]:
        with self._lock:
            was_visible = self._live_display is not None
            self._suspended += 1
            self._cancel_slot_render()
            self._cancel_reveal()
            self._clear_live()
        try:
            yield
        finally:
            with self._lock:
                self._suspended -= 1
                if not self._suspended and not self._closed:
                    if self._deferred_output is not None:
                        self._console.print(self._deferred_output)
                        self._deferred_output = None
                    elif self._terminal:
                        if was_visible and self._selected_state() is not None:
                            self._reveal_live()
                        else:
                            self._render_terminal()
                    else:
                        self._render_slots()

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            if event.kind != "prepare":
                super().__call__(event)
                return
            if self._closed or self._preparation_done:
                return
            if self._failure_event is not None:
                if event.subject == "state" and event.complete:
                    if event.status == "ok":
                        self.complete_preparation()
                    else:
                        self._end_preparation()
                    return
                if self._failure_event.subject != "cap":
                    return
            if event.status == "failed":
                if event.subject == "cap":
                    if self._failure_event is None:
                        self._failure_event = event
                    self._finish_item(event)
                    self._render_slots()
                else:
                    self._slots.clear()
                    self._cancel_slot_render()
                    super().__call__(event)
                return
            if event.status == "pending":
                self._finished.discard(self._work_key(event))
                return
            if event.cached or event.status == "skipped":
                return
            if event.subject == "state" and event.complete:
                self.complete_preparation()
                return
            if event.subject is None:
                return
            if event.status == "running":
                if self._started is None:
                    self._started = self._clock()
                if event.subject == "state":
                    return
                self._agent_work |= event.subject == "agent"
                key = self._work_key(event)
                if key in self._finished:
                    return
                if event.subject == "cap":
                    self._caps.add(key)
                index = next(
                    (
                        i
                        for i, item in enumerate(self._slots)
                        if item and item[0] == key
                    ),
                    None,
                )
                if index is None:
                    index = next(
                        (i for i, item in enumerate(self._slots) if item is None),
                        len(self._slots),
                    )
                    if index == len(self._slots):
                        self._slots.append(None)
                self._slots[index] = (key, event)
            elif event.complete:
                self._finish_item(event)
            else:
                return
            self._render_slots()

    def _finish_item(self, event: ProgressEvent) -> None:
        key = self._work_key(event)
        self._finished.add(key)
        for i, item in enumerate(self._slots):
            if item is not None and item[0] == key:
                self._slots[i] = None

    @staticmethod
    def _work_key(event: ProgressEvent) -> tuple[str | None, str]:
        scope = event.stage if event.subject == "agent" else event.scope
        return scope, event.id

    def _render_slots(self) -> None:
        if not self._enabled or self._suspended:
            return
        if not self._terminal:
            for item in self._slots:
                if item is None:
                    continue
                text = self._slot_text(item[1])
                key = ("prepare", repr(item[0]))
                previous = self._items.get(key)
                if previous is None or previous.event.stage != item[1].stage:
                    print(text, file=self._stream)
                    self._items[key] = _ItemState(item[1], 0)
            return
        if not any(self._slots):
            self._cancel_reveal()
            self._clear_live()
            return
        if self._live_display is None:
            self._render_terminal()
        elif self._clock() - self._last_slot_render >= 0.1:
            self._last_slot_render = self._clock()
            self._live_display.update(self._live_text(), refresh=True)
        elif self._slot_timer is None:
            self._slot_timer = Timer(0.1, self._refresh_slots)
            self._slot_timer.daemon = True
            self._slot_timer.start()

    def _render_terminal(self) -> None:
        if not self._suspended:
            super()._render_terminal()

    def _render_plain(self, event: ProgressEvent, previous: _ItemState | None) -> None:
        if not self._suspended:
            super()._render_plain(event, previous)

    def _reveal_live(self) -> None:
        with self._lock:
            if self._suspended:
                self._reveal_timer = None
                return
            super()._reveal_live()

    def _refresh_slots(self) -> None:
        with self._lock:
            self._slot_timer = None
            if not self._closed:
                self._render_slots()

    def _cancel_slot_render(self) -> None:
        if self._slot_timer is not None:
            self._slot_timer.cancel()
            self._slot_timer = None

    def _slot_text(self, event: ProgressEvent) -> str:
        action = {
            "resolve": "resolving",
            "fetch": "fetching",
            "parse": "parsing",
            "materialize": "updating",
        }[event.stage]
        width = self._available_width()
        suffix = f" {action}"
        if width <= len(suffix):
            return truncate(action, width)
        return truncate(one_line(event.ref or event.id), width - len(suffix)) + suffix

    def _selected_state(self) -> _ItemState | None:
        for item in self._slots:
            if item is not None:
                return _ItemState(item[1], 0)
        return super()._selected_state()

    def _live_text(self) -> Text:
        if any(self._slots):
            try:
                height = os.get_terminal_size(self._stream.fileno()).lines
            except (AttributeError, OSError, ValueError):
                height = 24
            rows = [
                self._slot_text(item[1]) for item in self._slots if item is not None
            ]
            return Text("\n".join(rows[: max(1, height - 1)]), style="dim")
        return super()._live_text()

    def _schedule_refresh(self) -> None:
        if not any(self._slots):
            super()._schedule_refresh()

    def complete_preparation(self) -> None:
        """Finish the CLI-owned phase without synthesizing domain progress."""

        try:
            with self._lock:
                if self._closed or self._preparation_done:
                    return
                self._end_preparation()
                if self._failure_event is not None:
                    self._print_or_defer(
                        Text(
                            self.failure_message(
                                RuntimeError("State contains rejected sources")
                            )
                        )
                    )
                else:
                    self._print_preparation_summary()
        except Exception:
            # Presentation is advisory, just like events sent through the sink.
            return

    def _end_preparation(self) -> None:
        self._preparation_done = True
        self._slots.clear()
        self._cancel_slot_render()
        self._cancel_reveal()
        self._clear_live()

    def _print_preparation_summary(self) -> None:
        if self._started is None or not self._enabled:
            return
        parts = []
        if self._agent_work:
            parts.append("agent")
        if self._caps:
            parts.append(
                f"{len(self._caps)} {'cap' if len(self._caps) == 1 else 'caps'}"
            )
        subject = " and ".join(parts) or "State"
        elapsed = format_duration(self._clock() - self._started)
        self._print_or_defer(Text(f"Prepared {subject} in {elapsed}", style="dim"))

    def _print_or_defer(self, text: Text) -> None:
        if self._suspended:
            self._deferred_output = text
        else:
            self._console.print(text)

    def close(self) -> None:
        with self._lock:
            self._cancel_slot_render()
            self._slots.clear()
            self._deferred_output = None
            super().close()


def make_cli_progress(
    *,
    stream: TextIO | None = None,
    enabled: bool = True,
    max_width: int | None = None,
    leading_gap: bool = False,
    preparation: bool = False,
) -> CliProgress:
    """Create one invocation-scoped operational presenter."""

    if (
        preparation
        and enabled
        and (current := _PREPARATION_PROGRESS.get()) is not None
        and current._enabled
        and not current._closed
        and not current._preparation_done
        and current._failure_event is None
    ):
        return current
    presenter = PreparationProgress if preparation else CliProgress
    return presenter(
        stream=stream,
        enabled=enabled,
        max_width=max_width,
        leading_gap=leading_gap,
    )


def runtime_startup_failure_message(
    progress: CliProgress,
    error: BaseException,
    *,
    log_path: Path | None = None,
    dev_artifact: Path | None = None,
    development_build: bool = False,
) -> str:
    """Describe one failed AgentServer startup without duplicating its cause."""

    fallback = progress.failure_reason or str(error).strip() or type(error).__name__
    reason, fix = _runtime_failure_guidance(
        progress.failure_label,
        fallback_reason=fallback,
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
    normalized = activity.casefold() if activity is not None else ""
    if "install toolang" in normalized:
        if dev_artifact is not None:
            return (
                f"Could not install Toolang from {dev_artifact.name}",
                "Rebuild the wheel and check the installation log",
            )
        return (
            "Could not install Toolang from the package index",
            "Check network access or use --dev PATH with a compatible wheel",
        )
    if "check toolang" in normalized or "checking toolang" in normalized:
        if dev_artifact is not None:
            return (
                "The selected Toolang wheel cannot start the required AgentServer",
                "Rebuild or select a compatible Toolang wheel, then run this command "
                "again with `--dev PATH`",
            )
        if development_build:
            return (
                "The Toolang package installed in the guest cannot start the required "
                "AgentServer",
                "Build the current source with `uv build --wheel`, then run this "
                "command again with `--dev dist`",
            )
        return (
            "The Toolang package installed in the guest cannot start the required "
            "AgentServer",
            "Build or select a compatible Toolang wheel, then run this command again "
            "with `--dev PATH`",
        )
    return fallback_reason, None


def _with_facts(label: str, facts: list[str]) -> str:
    if not facts:
        return label
    running = label.endswith("...")
    base = label[:-3].rstrip() if running else label
    suffix = f" ({', '.join(facts)})"
    return f"{base}{suffix}{'...' if running else ''}"


def _bounded_detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    normalized = one_line(detail)
    if not normalized:
        return None
    if len(normalized) <= _MAX_DETAIL:
        return normalized
    return normalized[: _MAX_DETAIL - 1] + "…"
