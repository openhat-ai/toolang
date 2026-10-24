"""Foreground deadlines remain bounded when async cleanup cannot cooperate."""

from contextlib import suppress
import subprocess
import sys
import time

import psutil


def test_deadline_kills_stuck_owner_and_its_tool_process(tmp_path):
    script = """
import subprocess, sys, time
from toolang.up import closing
closing.FINALIZATION_GRACE_SEC = 0.4
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
print(child.pid, flush=True)
sys.stdin.readline()
deadline = closing.ClosingDeadline()
deadline.begin('script interrupted')
time.sleep(0.2)
deadline.begin('second interruption must not reset the deadline')
while True:
    time.sleep(10)
"""
    worker = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child = None
    try:
        assert worker.stdout is not None
        child = psutil.Process(int(worker.stdout.readline()))
        started = time.monotonic()
        assert worker.stdin is not None
        worker.stdin.write("begin\n")
        worker.stdin.flush()
        _stdout, stderr = worker.communicate(timeout=3)
        assert worker.returncode == 124
        assert "records may be incomplete" in stderr
        assert time.monotonic() - started < 2
        with suppress(psutil.NoSuchProcess):
            assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=3)
        with suppress(psutil.NoSuchProcess):
            if (
                child is not None
                and child.is_running()
                and child.status() != psutil.STATUS_ZOMBIE
            ):
                child.kill()
