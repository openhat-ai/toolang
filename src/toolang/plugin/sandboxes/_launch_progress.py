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

_EVENTS: dict[str, tuple[ProgressKind, ProgressStage, str, ProgressStatus] | None] = {
    "install.running": ("runtime", "create", "Installing Toolang", "running"),
    "install.ok": None,
    "install.failed": ("runtime", "create", "Installing Toolang", "failed"),
    "validate.running": (
        "runtime",
        "create",
        "Checking Toolang compatibility",
        "running",
    ),
    "validate.ok": None,
    "validate.failed": (
        "runtime",
        "create",
        "Checking Toolang compatibility",
        "failed",
    ),
    "server.running": None,
    "setup.load.running": ("setup", "load", "Loading agent setup", "running"),
    "setup.load.ok": ("setup", "load", "Loading agent setup", "ok"),
    "setup.load.failed": ("setup", "load", "Loading agent setup", "failed"),
    "setup.discover.running": (
        "setup",
        "discover",
        "Discovering models",
        "running",
    ),
    "setup.discover.ok": ("setup", "discover", "Discovering models", "ok"),
    "setup.discover.failed": (
        "setup",
        "discover",
        "Discovering models",
        "failed",
    ),
}
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
            event = _EVENTS[token]
            if event is not None:
                kind, stage, label, status = event
                emit_progress(
                    progress,
                    id=(
                        runtime_progress_id if kind == "runtime" else setup_progress_id
                    ),
                    kind=kind,
                    stage=stage,
                    label=label,
                    status=status,
                    detail=_event_detail(token, package_source=package_source),
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


def _event_detail(token: str, *, package_source: str) -> str | None:
    if token == "install.running":
        return package_source
    if token == "install.failed":
        return "Toolang installation failed."
    if token == "validate.failed":
        return "The installed Toolang package cannot start the required AgentServer."
    return None


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
