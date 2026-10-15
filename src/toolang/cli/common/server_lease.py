"""In-process retention for an acquired automatic server."""

from __future__ import annotations

import os
import signal
import threading
import time
from uuid import uuid4

import httpx

from toolang.up.lifecycle import LEASE_RENEW_SEC, LEASE_TTL_SEC
from toolang.up.types import AgentServerRef


class ServerLeaseLostError(RuntimeError):
    """A caller must stop submitting work after losing its retention lease."""


class ServerLease:
    def __init__(self, server: AgentServerRef) -> None:
        self.server = server
        self.client_id = f"client_{uuid4().hex}"
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._interrupt = threading.current_thread() is threading.main_thread()
        self._thread = threading.Thread(target=self._renew, daemon=True)
        self._last_success = time.monotonic()

    def _request(self, method: str) -> None:
        with httpx.Client(timeout=3.0) as http:
            response = http.request(
                method,
                f"{self.server.endpoint}/api/v1/runtime/leases/{self.client_id}",
                json={"launch_id": self.server.launch_id},
            )
            response.raise_for_status()
        self._last_success = time.monotonic()

    def register(self) -> None:
        self._request("PUT")

    def start(self) -> None:
        self._thread.start()

    def check(self) -> None:
        if self._lost.is_set():
            raise ServerLeaseLostError(
                "server retention lease was lost; accepted runs may still be running. "
                "Inspect their IDs before starting another command."
            )

    def _renew(self) -> None:
        while not self._stop.wait(LEASE_RENEW_SEC):
            try:
                self._request("PUT")
            except httpx.HTTPError:
                if time.monotonic() - self._last_success < LEASE_TTL_SEC:
                    continue
                self._lost.set()
                if self._interrupt and not self._stop.is_set():
                    os.kill(os.getpid(), signal.SIGINT)
                return

    def close(self) -> None:
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join()
        try:
            self._request("DELETE")
        except httpx.HTTPError:
            pass
