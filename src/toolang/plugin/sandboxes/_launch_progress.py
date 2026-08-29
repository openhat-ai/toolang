"""Observe the closed launch-progress token stream shared with sandbox guests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import os
from pathlib import Path
import stat

from toolang.base.types.progress import (
    ProgressKind,
    ProgressSink,
    ProgressStage,
    ProgressStatus,
)
from toolang.common.progress import emit_progress


POLL_INTERVAL_SEC = 0.05
MAX_BYTES = 1024

_ProgressTuple = tuple[ProgressKind, ProgressStage, str, ProgressStatus, str | None]
_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"install.running", "server.running", "setup.load.running"}),
    "install.running": frozenset({"install.ok", "install.failed"}),
    "install.ok": frozenset({"validate.running"}),
    "validate.running": frozenset({"validate.ok", "validate.failed"}),
    "validate.ok": frozenset({"server.running", "setup.load.running"}),
    "server.running": frozenset({"setup.load.running"}),
    "setup.load.running": frozenset({"setup.load.ok", "setup.load.failed"}),
    "setup.load.ok": frozenset({"setup.discover.running"}),
    "setup.discover.running": frozenset({"setup.discover.ok", "setup.discover.failed"}),
}
_TERMINAL = frozenset(
    {
        "install.failed",
        "validate.failed",
        "setup.load.failed",
        "setup.discover.ok",
        "setup.discover.failed",
    }
)


async def observe_launch_progress(
    path: Path,
    *,
    progress: ProgressSink,
    runtime_progress_id: str,
    setup_progress_id: str,
    package_source: str,
    running: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    """Forward valid tokens until initial setup or the workload is terminal."""

    processed_lines = 0
    previous: str | None = None
    stopped_observed = False
    while True:
        content = await asyncio.to_thread(read_launch_progress, path)
        lines = content.splitlines()
        complete_count = (
            len(lines) if content.endswith("\n") else max(len(lines) - 1, 0)
        )
        for token in lines[processed_lines:complete_count]:
            if token not in _TRANSITIONS.get(previous, frozenset()):
                continue
            previous = token
            for kind, stage, label, status, detail in _token_events(
                token,
                package_source=package_source,
            ):
                emit_progress(
                    progress,
                    id=(
                        runtime_progress_id if kind == "runtime" else setup_progress_id
                    ),
                    kind=kind,
                    stage=stage,
                    label=label,
                    status=status,
                    detail=detail,
                )
            if token in _TERMINAL:
                return
        processed_lines = complete_count
        if running is not None and not await running():
            if stopped_observed:
                return
            stopped_observed = True
        else:
            stopped_observed = False
        await asyncio.sleep(POLL_INTERVAL_SEC)


def _token_events(token: str, *, package_source: str) -> tuple[_ProgressTuple, ...]:
    source = (
        "the package index" if package_source == "package index" else package_source
    )
    events: dict[str, tuple[_ProgressTuple, ...]] = {
        "install.running": (
            (
                "runtime",
                "create",
                f"Installing Toolang from {source}...",
                "running",
                None,
            ),
        ),
        "install.ok": (
            ("runtime", "create", f"Installed Toolang from {source}", "running", None),
        ),
        "install.failed": (
            (
                "runtime",
                "create",
                "Failed to install Toolang",
                "failed",
                "Toolang installation failed",
            ),
        ),
        "validate.running": (
            ("runtime", "create", "Checking Toolang...", "running", None),
        ),
        "validate.ok": (("runtime", "create", "Checked Toolang", "running", None),),
        "validate.failed": (
            (
                "runtime",
                "create",
                "Failed to check Toolang",
                "failed",
                "The installed Toolang package cannot start the required AgentServer",
            ),
        ),
        "server.running": (
            ("runtime", "create", "Created runtime", "ok", None),
            ("runtime", "start", "Starting agent...", "running", None),
        ),
        "setup.load.running": (("setup", "load", "Loading setup...", "running", None),),
        "setup.load.ok": (("setup", "load", "Loaded setup", "ok", None),),
        "setup.load.failed": (
            ("setup", "load", "Failed to load setup", "failed", None),
        ),
        "setup.discover.running": (
            ("setup", "discover", "Discovering models...", "running", None),
        ),
        "setup.discover.ok": (("setup", "discover", "Discovered models", "ok", None),),
        "setup.discover.failed": (
            ("setup", "discover", "Failed to discover models", "failed", None),
        ),
    }
    return events.get(token, ())


def read_launch_progress(path: Path) -> str:
    """Read one bounded, regular, non-symlink launch token file."""

    try:
        expected = os.lstat(path)
    except OSError:
        return ""
    if not stat.S_ISREG(expected.st_mode):
        return ""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return ""
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (expected.st_dev, expected.st_ino):
            return ""
        if opened.st_size > MAX_BYTES:
            return ""
        return os.read(descriptor, MAX_BYTES + 1).decode("ascii", errors="ignore")
    finally:
        os.close(descriptor)
