"""Terminal presentation for command-owned AgentServer cleanup."""

from __future__ import annotations

import sys
from threading import RLock
import time
from typing import TextIO

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.text import Text

from toolang.base.types.progress import ProgressEvent


_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_MAX_DETAIL = 80


class RuntimeShutdownProgress:
    """Render command-owned runtime cleanup without affecting stdout."""

    def __init__(
        self,
        agent: str,
        sandbox: str,
        *,
        stream: TextIO | None = None,
        live: bool | None = None,
    ) -> None:
        self.agent = agent
        self.sandbox = sandbox
        self._stream = stream or sys.stderr
        stream_is_tty = bool(getattr(self._stream, "isatty", lambda: False)())
        self._live_enabled = stream_is_tty if live is None else live
        self._console = Console(
            file=self._stream,
            force_terminal=True if self._live_enabled else None,
            color_system="standard" if self._live_enabled else "auto",
            highlight=False,
        )
        self._display: Live | None = None
        self._started_at = time.monotonic()
        self._current_stage: str | None = None
        self._current_detail: str | None = None
        self._printed_phases: set[str] = set()
        self._finished = False
        self._lock = RLock()

    @property
    def current_stage(self) -> str | None:
        with self._lock:
            return self._current_stage

    def __call__(self, event: ProgressEvent) -> None:
        if not event.phase.startswith("shutdown."):
            return
        with self._lock:
            if self._finished:
                return
            detail = _bounded_detail(_normalized_detail(event.detail))
            if event.status in {"running", "failed"}:
                self._current_stage = event.label
                self._current_detail = detail
            if self._live_enabled:
                self._render_live()
            elif event.status == "running" and event.phase not in self._printed_phases:
                self._printed_phases.add(event.phase)
                suffix = f": {detail}" if detail else ""
                print(f"{event.label}{suffix}", file=self._stream)

    def finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            display = self._display
            if display is not None:
                display.stop()
                self._display = None
            if display is not None and self._console.is_terminal:
                self._stream.write("\x1b[0m")
                self._stream.flush()

    def interrupt(self) -> None:
        self.finish()

    def _render_live(self) -> None:
        if self._current_stage is None:
            return
        if self._display is None:
            self._display = Live(
                _ShutdownLine(self),
                console=self._console,
                refresh_per_second=10,
                transient=True,
            )
            self._display.start(refresh=True)
            return
        self._display.refresh()

    def _text(self) -> Text:
        with self._lock:
            elapsed = max(time.monotonic() - self._started_at, 0)
            spinner = _SPINNER[int(elapsed * 10) % len(_SPINNER)]
            parts = [
                f"{spinner} Cleaning up temporary agent {self.agent}",
                self.sandbox,
                self._current_stage or "Stopping",
            ]
            if self._current_detail and self._current_detail != self.sandbox:
                parts.append(self._current_detail)
            parts.append(_elapsed(elapsed))
            return Text(" · ".join(parts), style="dim")


class _ShutdownLine:
    def __init__(self, progress: RuntimeShutdownProgress) -> None:
        self._progress = progress

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        del console, options
        yield self._progress._text()


def make_runtime_shutdown_progress(
    agent: str,
    sandbox: str,
    *,
    live: bool | None = None,
) -> RuntimeShutdownProgress:
    """Create the standard command-owned runtime cleanup presenter."""

    return RuntimeShutdownProgress(agent, sandbox, live=live)


def _bounded_detail(detail: str | None) -> str | None:
    if detail is None or len(detail) <= _MAX_DETAIL:
        return detail
    return detail[: _MAX_DETAIL - 1] + "…"


def _normalized_detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    normalized = " ".join(detail.split())
    return normalized or None


def _elapsed(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{int(seconds)}s"
