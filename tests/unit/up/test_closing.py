"""Foreground deadlines remain bounded when async cleanup cannot cooperate."""

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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child = None
    try:
        assert worker.stdout is not None
        child = psutil.Process(int(worker.stdout.readline()))
        started = time.monotonic()
        _stdout, stderr = worker.communicate(timeout=3)
        assert worker.returncode == 124
        assert "records may be incomplete" in stderr
        assert time.monotonic() - started < 2
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=3)
        if (
            child is not None
            and child.is_running()
            and child.status() != psutil.STATUS_ZOMBIE
        ):
            child.kill()
