"""Retention failure does not turn transport loss into run cancellation."""

import threading

import httpx
import pytest

from toolang.cli.common import server_lease
from toolang.up.types import AgentServerRef


def _lease_http(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.parametrize("promoted_at_registration", [False, True])
def test_promotion_ends_renewal_and_retention_failure_checks(
    monkeypatch, promoted_at_registration
):
    now = [0.0]
    requests = []
    monkeypatch.setattr(server_lease.time, "monotonic", lambda: now[0])
    lease = server_lease.ServerLease(
        AgentServerRef("host", "http://server", launch_id="launch")
    )
    lease._interrupt = False
    promotion_request = 1 if promoted_at_registration else 2

    def respond(request):
        requests.append(request.method)
        if len(requests) > promotion_request:
            raise httpx.ConnectError("persistent servers need no retention transport")
        return httpx.Response(
            200,
            json={
                "launch_id": "launch",
                "lifetime": "persistent"
                if len(requests) == promotion_request
                else "automatic",
                "lifecycle_protocol": 1,
            },
        )

    def wait(seconds):
        now[0] += seconds
        return lease._stop.is_set()

    _lease_http(monkeypatch, respond)
    monkeypatch.setattr(lease._stop, "wait", wait)
    lease.register()
    lease._renew()
    now[0] += 100
    lease.check()
    assert requests == ["PUT"] * promotion_request


def test_expired_client_cannot_keep_submitting_before_renewal_thread_runs(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(server_lease.time, "monotonic", lambda: now[0])
    lease = server_lease.ServerLease(
        AgentServerRef("host", "http://server", launch_id="launch")
    )
    now[0] = 16.0
    with pytest.raises(server_lease.ServerLeaseLostError):
        lease.check()
    monkeypatch.setattr(lease, "_request", lambda _method: None)
    lease.close()
    with pytest.raises(server_lease.ServerLeaseLostError):
        lease.check()


def test_late_renewal_response_cannot_revive_expired_retention(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(server_lease.time, "monotonic", lambda: now[0])
    lease = server_lease.ServerLease(
        AgentServerRef("host", "http://server", launch_id="launch")
    )

    def respond(request):
        now[0] = 16.0
        return httpx.Response(
            200,
            json={
                "launch_id": "launch",
                "lifetime": "automatic",
                "lifecycle_protocol": 1,
            },
        )

    _lease_http(monkeypatch, respond)
    with pytest.raises(server_lease.ServerLeaseLostError):
        lease.register()
    with pytest.raises(server_lease.ServerLeaseLostError):
        lease.check()


def test_slow_release_does_not_expire_a_successfully_completed_client(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(server_lease.time, "monotonic", lambda: now[0])
    lease = server_lease.ServerLease(
        AgentServerRef("host", "http://server", launch_id="launch")
    )

    def fail(_method):
        now[0] = 100.0
        raise httpx.ConnectError("server has already exited")

    monkeypatch.setattr(lease, "_request", fail)
    lease.close()
    lease.check()


def test_failed_release_is_best_effort(monkeypatch):
    lease = server_lease.ServerLease(
        AgentServerRef("host", "http://server", launch_id="launch")
    )

    def fail(_method):
        raise httpx.ConnectError("server has already exited")

    monkeypatch.setattr(lease, "_request", fail)
    lease.close()
    lease.check()


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("renewal failed"), ValueError("invalid response")]
)
def test_inflight_renewal_failure_after_close_does_not_report_lease_loss(
    monkeypatch, error
):
    now = [0.0]
    entered = threading.Event()
    monkeypatch.setattr(server_lease.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(server_lease, "LEASE_RENEW_SEC", 0)
    lease = server_lease.ServerLease(
        AgentServerRef("host", "http://server", launch_id="launch")
    )
    lease._interrupt = False
    requests = []

    def request(method):
        requests.append(method)
        if method == "PUT":
            entered.set()
            assert lease._stop.wait(2)
            now[0] = 100.0
            raise error

    monkeypatch.setattr(lease, "_request", request)
    lease.start()
    try:
        assert entered.wait(2)
    finally:
        lease.close()
    lease.check()
    assert requests == ["PUT", "DELETE"]


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
    assert requests == ["PUT", "PUT"]
    with pytest.raises(RuntimeError, match="accepted runs may still be running"):
        lease.check()
