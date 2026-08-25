"""Reusable pseudo-terminal driver for chat TUI system tests."""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time

from tests import PROJECT_ROOT

_CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


class ChatTuiPtySession:
    """Run a Python chat test entry point behind a real pseudo-terminal."""

    def __init__(
        self,
        *,
        master: int,
        process: subprocess.Popen[bytes],
    ) -> None:
        self.master = master
        self.process = process
        self.data = bytearray()

    @classmethod
    def start(
        cls,
        module: str,
        *arguments: str | Path,
        rows: int = 30,
        columns: int = 100,
    ) -> ChatTuiPtySession:
        master, slave = pty.openpty()
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, columns, 0, 0),
        )
        environment = dict(os.environ)
        environment.update(
            {
                "COLUMNS": str(columns),
                "LINES": str(rows),
                "PROMPT_TOOLKIT_NO_CPR": "1",
                "PYTHONUNBUFFERED": "1",
                "TERM": "xterm-256color",
            }
        )
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    module,
                    *(str(argument) for argument in arguments),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
            )
        finally:
            os.close(slave)
        return cls(master=master, process=process)

    @property
    def output(self) -> str:
        data = _OSC.sub(b"", bytes(self.data))
        data = _CSI.sub(b"", data)
        return data.decode("utf-8", errors="replace").replace("\r", "")

    def send(self, value: bytes) -> None:
        os.write(self.master, value)

    def wait_for(self, *values: str, timeout: float = 10) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            output = self.output
            if all(value in output for value in values):
                return output
            self._read(timeout=min(0.1, max(deadline - time.monotonic(), 0)))
            if self.process.poll() is not None:
                self._read(timeout=0)
                break
        expected = ", ".join(repr(value) for value in values)
        raise AssertionError(f"PTY output did not contain {expected}:\n{self.output}")

    def wait_for_exit(self, timeout: float = 10) -> int:
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            self._read(timeout=0.1)
        if self.process.poll() is None:
            raise AssertionError(f"PTY process did not exit:\n{self.output}")
        self._read(timeout=0)
        return self.process.returncode

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        try:
            os.close(self.master)
        except OSError:
            pass

    def _read(self, *, timeout: float) -> None:
        readable, _, _ = select.select([self.master], [], [], timeout)
        if not readable:
            return
        try:
            chunk = os.read(self.master, 65_536)
        except OSError as exc:
            if exc.errno == errno.EIO:
                return
            raise
        self.data.extend(chunk)
