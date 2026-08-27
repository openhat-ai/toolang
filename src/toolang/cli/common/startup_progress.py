"""Terminal presentation for AgentServer startup progress."""

from __future__ import annotations

import sys
from pathlib import Path
from threading import RLock
import time
from typing import TextIO

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.text import Text

from toolang.base.types.progress import ProgressEvent


_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_MAX_DETAIL = 80
_PHASE_ORDER = {
    "startup.prepare": 0,
    "startup.launch": 1,
    "startup.install": 2,
    "startup.validate": 3,
    "startup.server": 4,
    "startup.ready": 5,
}


class RuntimeStartupProgress:
    """Render one startup stage without affecting stdout contracts."""

    def __init__(
        self,
        agent: str,
        sandbox: str,
        *,
        stream: TextIO | None = None,
        live: bool | None = None,
        enabled: bool = True,
    ) -> None:
        self.agent = agent
        self.sandbox = sandbox
        self._stream = stream or sys.stderr
        stream_is_tty = bool(getattr(self._stream, "isatty", lambda: False)())
        self._live_enabled = stream_is_tty if live is None else live
        self._enabled = enabled
        self._console = Console(
            file=self._stream,
            force_terminal=True if self._live_enabled else None,
            color_system="standard" if self._live_enabled else "auto",
            highlight=False,
        )
        self._display: Live | None = None
        self._started_at = time.monotonic()
        self._current_stage: str | None = None
        self._current_phase: str | None = None
        self._current_running = False
        self._current_detail: str | None = None
        self._failure_reason: str | None = None
        self._failure_phase: str | None = None
        self._printed_phases: set[str] = set()
        self._finished = False
        self._lock = RLock()

    @property
    def current_stage(self) -> str | None:
        with self._lock:
            return self._current_stage

    @property
    def failure_reason(self) -> str | None:
        with self._lock:
            return self._failure_reason

    @property
    def failure_phase(self) -> str | None:
        with self._lock:
            return self._failure_phase

    def __call__(self, event: ProgressEvent) -> None:
        if not event.phase.startswith("startup."):
            return
        with self._lock:
            if self._finished:
                return
            if self._failure_phase is not None:
                return
            reason = _normalized_detail(event.detail)
            detail = _bounded_detail(reason)
            if (
                event.status == "running"
                and self._current_phase is not None
                and _PHASE_ORDER.get(event.phase, -1)
                < _PHASE_ORDER.get(self._current_phase, -1)
            ):
                return
            if (
                event.status == "failed"
                and self._current_running
                and self._current_phase is not None
                and _PHASE_ORDER.get(event.phase, -1)
                < _PHASE_ORDER.get(self._current_phase, -1)
            ):
                self._failure_reason = reason
                self._failure_phase = event.phase
                return
            if event.status in {"running", "failed"}:
                self._current_stage = event.label
                self._current_phase = event.phase
                self._current_detail = detail
                self._current_running = event.status == "running"
            elif event.status == "ok" and event.phase == self._current_phase:
                self._current_running = False
            if event.status == "failed":
                self._failure_reason = reason
                self._failure_phase = event.phase
            if not self._enabled:
                return
            if self._live_enabled:
                self._render_live()
            elif event.status == "running" and event.phase not in self._printed_phases:
                self._printed_phases.add(event.phase)
                suffix = f": {self._current_detail}" if self._current_detail else ""
                print(f"{event.label}{suffix}", file=self._stream)

    def finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            if self._display is not None:
                self._display.stop()
                self._display = None
            if self._enabled and self._console.is_terminal:
                self._stream.write("\x1b[0m")
                self._stream.flush()

    def interrupt(self) -> None:
        self.finish()

    def _render_live(self) -> None:
        if self._current_stage is None:
            return
        if self._display is None:
            self._display = Live(
                _StartupLine(self),
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
                f"{spinner} Starting agent {self.agent}",
                self.sandbox,
                self._current_stage or "Starting",
            ]
            if self._current_detail and self._current_detail != self.sandbox:
                parts.append(self._current_detail)
            parts.append(_elapsed(elapsed))
            return Text(" · ".join(parts), style="dim")


class _StartupLine:
    def __init__(self, progress: RuntimeStartupProgress) -> None:
        self._progress = progress

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        del console, options
        yield self._progress._text()


def make_runtime_startup_progress(
    agent: str,
    sandbox: str,
    *,
    live: bool | None = None,
    enabled: bool = True,
) -> RuntimeStartupProgress:
    """Create the standard runtime-startup presenter."""

    return RuntimeStartupProgress(agent, sandbox, live=live, enabled=enabled)


def runtime_startup_failure_message(
    name: str,
    sandbox: str,
    progress: RuntimeStartupProgress,
    error: BaseException,
    *,
    log_path: Path | None = None,
    dev_artifact: Path | None = None,
    development_build: bool = False,
) -> str:
    """Describe one failed AgentServer startup with its active stage."""

    fallback_reason = (
        progress.failure_reason or str(error).strip() or type(error).__name__
    )
    reason, fix = _startup_failure_guidance(
        progress.failure_phase,
        fallback_reason=fallback_reason,
        dev_artifact=dev_artifact,
        development_build=development_build,
    )
    stage = progress.current_stage or "Starting agent"
    lines = [
        f"Could not start agent {name} in {sandbox}",
        f"Stage: {stage}",
        f"Reason: {reason}",
    ]
    if fix is not None:
        lines.append(f"Fix: {fix}")
    if log_path is not None:
        lines.append(f"Log: {log_path}")
    return "\n".join(lines)


def _startup_failure_guidance(
    phase: str | None,
    *,
    fallback_reason: str,
    dev_artifact: Path | None,
    development_build: bool,
) -> tuple[str, str | None]:
    if phase == "startup.install":
        if dev_artifact is not None:
            return (
                f"Could not install Toolang from {dev_artifact.name}.",
                "Rebuild the wheel and check the installation log.",
            )
        return (
            "Could not install Toolang from the package index.",
            "Check the log and network access, or run this command again with a "
            "local wheel using `--dev PATH`.",
        )
    if phase == "startup.validate":
        if dev_artifact is not None:
            return (
                "The selected Toolang wheel cannot start the required AgentServer.",
                "Rebuild or select a compatible Toolang wheel, then run this command "
                "again with `--dev PATH`.",
            )
        if development_build:
            return (
                "The Toolang package installed in the guest cannot start the required "
                "AgentServer.",
                "Build the current source with `uv build --wheel`, then run this "
                "command again with `--dev dist`.",
            )
        return (
            "The Toolang package installed in the guest cannot start the required "
            "AgentServer.",
            "Build or select a compatible Toolang wheel, then run this command again "
            "with `--dev PATH`.",
        )
    return fallback_reason, None


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
