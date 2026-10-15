"""Retention failure does not turn transport loss into run cancellation."""

import httpx
import pytest

from toolang.cli.common import server_lease
from toolang.up.types import AgentServerRef


def test_failed_release_is_best_effort(monkeypatch):
    lease = server_lease.ServerLease(
        AgentServerRef("host", "http://server", launch_id="launch")
    )

    def fail(_method):
        raise httpx.ConnectError("server has already exited")

    monkeypatch.setattr(lease, "_request", fail)
    lease.close()
    lease.check()


def test_renewal_loss_expires_without_cancel_or_retrying_execution(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(server_lease.time, "monotonic", lambda: now[0])
    lease = server_lease.ServerLease(
        AgentServerRef("host", "http://server", launch_id="launch")
    )
    lease._interrupt = False
    requests = []

    def wait(seconds):
        now[0] += seconds
        return False

    def fail(method):
        requests.append(method)
        raise httpx.ConnectError("connection lost")

    monkeypatch.setattr(lease._stop, "wait", wait)
    monkeypatch.setattr(lease, "_request", fail)
    lease._renew()
    assert requests == ["PUT", "PUT", "PUT"]
    with pytest.raises(RuntimeError, match="accepted runs may still be running"):
        lease.check()
