"""Initial AgentServer setup progress transport."""

from __future__ import annotations

import os
from pathlib import Path
import stat

from toolang.base.types.progress import ProgressEvent, ProgressSink
from toolang.common.progress import LAUNCH_PROGRESS_FILE_ENV


_MAX_BYTES = 1024
_SETUP_TOKENS = frozenset(
    {
        "setup.load.running",
        "setup.load.ok",
        "setup.load.failed",
        "setup.discover.running",
        "setup.discover.ok",
        "setup.discover.failed",
    }
)


def launch_progress_sink(environ: dict[str, str]) -> ProgressSink | None:
    """Return a closed-token sink for one controller-created launch file."""

    raw_path = environ.get(LAUNCH_PROGRESS_FILE_ENV)
    if raw_path is None or not raw_path.strip():
        return None
    path = Path(raw_path)

    def write(event: ProgressEvent) -> None:
        if event.kind != "setup":
            return
        token = f"{event.kind}.{event.stage}.{event.status}"
        if token in _SETUP_TOKENS:
            _append_token(path, token)

    return write


def _append_token(path: Path, token: str) -> None:
    try:
        expected = os.lstat(path)
    except OSError:
        return
    if not stat.S_ISREG(expected.st_mode) or expected.st_size > _MAX_BYTES:
        return
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        opened = os.fstat(descriptor)
        payload = f"{token}\n".encode("ascii")
        if (
            stat.S_ISREG(opened.st_mode)
            and (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino)
            and opened.st_size + len(payload) <= _MAX_BYTES
        ):
            os.write(descriptor, payload)
    except OSError:
        return
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
