"""CLI interruption bounds setup cleanup before a Run has been admitted."""

import signal
import subprocess
import sys
import time


def test_setup_interrupt_starts_deadline_before_stuck_async_cleanup(tmp_path):
    code = """
import asyncio
from toolang.cli.common.foreground import run_foreground
from toolang.up import closing
closing.FINALIZATION_GRACE_SEC = 0.5
async def setup():
    print('ready', flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        print('closing', flush=True)
        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                pass
run_foreground(setup(), closing=closing.ClosingDeadline())
"""
    worker = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert worker.stdout is not None
        assert worker.stdout.readline().strip() == "ready"
        started = time.monotonic()
        worker.send_signal(signal.SIGINT)
        assert worker.stdout.readline().strip() == "closing"
        worker.send_signal(signal.SIGINT)
        _stdout, stderr = worker.communicate(timeout=3)
        assert worker.returncode == 124
        assert "records may be incomplete" in stderr
        assert time.monotonic() - started < 2
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=3)
