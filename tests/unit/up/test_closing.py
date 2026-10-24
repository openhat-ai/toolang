"""Foreground deadlines remain bounded when async cleanup cannot cooperate."""

from contextlib import suppress
import subprocess
import sys
import time

import psutil
import pytest

from toolang.up.closing import ClosingDeadline


def test_arming_before_status_io_can_only_shorten_the_closing_budget():
    notifications = []
    deadline = ClosingDeadline(
        enabled=False, on_begin=lambda *args: notifications.append(args)
    )
    deadline.arm(started_at=100)
    deadline.arm(started_at=200)
    assert deadline.started == 100
    assert notifications == []
    deadline.begin("script interrupted", started_at=90)
    deadline.begin("second interruption", started_at=80)
    assert deadline.started == 90
    assert deadline.reason == "script interrupted"
    assert notifications == [(90, "script interrupted")]


@pytest.mark.parametrize("entry", ["begin", "arm"])
def test_deadline_kills_stuck_owner_and_its_tool_process(tmp_path, entry):
    script = """
import subprocess, sys, time
from toolang.up import closing
closing.FINALIZATION_GRACE_SEC = 0.4
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
print(child.pid, flush=True)
sys.stdin.readline()
deadline = closing.ClosingDeadline()
if sys.argv[1] == 'arm':
    deadline.arm()
else:
    deadline.begin('script interrupted')
time.sleep(0.2)
deadline.begin('second interruption must not reset the deadline')
while True:
    time.sleep(10)
"""
    worker = subprocess.Popen(
        [sys.executable, "-c", script, entry],
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
            # The owner can exit before the kernel finishes terminating its child.
            _, alive = psutil.wait_procs([child], timeout=1)
            assert not alive or child.status() == psutil.STATUS_ZOMBIE
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
