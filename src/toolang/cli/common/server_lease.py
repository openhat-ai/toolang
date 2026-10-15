"""In-process retention for an acquired automatic server."""

from __future__ import annotations

import os
import signal
import threading
import time
from uuid import uuid4

import httpx

from toolang.api.schemas import RuntimeLifecyclePayload
from toolang.up.lifecycle import LEASE_RENEW_SEC, LEASE_TTL_SEC
from toolang.up.types import AgentServerRef


class ServerLeaseLostError(RuntimeError):
    """A caller must stop submitting work after losing its retention lease."""


class ServerLease:
    def __init__(self, server: AgentServerRef) -> None:
        self.server = server
        self.client_id = f"client_{uuid4().hex}"
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._interrupt = threading.current_thread() is threading.main_thread()
        self._thread = threading.Thread(target=self._renew, daemon=True)
        self._expires_at = time.monotonic() + LEASE_TTL_SEC

    def _request(self, method: str) -> None:
        issued_at = time.monotonic()
        with httpx.Client(timeout=3.0) as http:
            response = http.request(
                method,
                f"{self.server.endpoint}/api/v1/runtime/leases/{self.client_id}",
                json={"launch_id": self.server.launch_id},
            )
            response.raise_for_status()
        if method == "PUT":
            payload = RuntimeLifecyclePayload.model_validate(response.json())
            if payload.launch_id != self.server.launch_id or payload.lifetime not in {
                "automatic",
                "persistent",
            }:
                raise ValueError("server retention response changed launch or lifetime")
            self.check()
            if payload.lifetime == "persistent":
                self._stop.set()
            else:
                # The server may have renewed at any point after request dispatch.
                self._expires_at = issued_at + LEASE_TTL_SEC
                self.check()

    def register(self) -> None:
        self._request("PUT")

    def start(self) -> None:
        self._thread.start()

    def check(self) -> None:
        with self._state_lock:
            if not self._stop.is_set() and time.monotonic() >= self._expires_at:
                self._lost.set()
            lost = self._lost.is_set()
        if lost:
            raise ServerLeaseLostError(
                "server retention lease was lost; accepted runs may still be running. "
                "Inspect their IDs before starting another command."
            )

    def _renew(self) -> None:
        while not self._stop.wait(LEASE_RENEW_SEC):
            try:
                self.check()
                self._request("PUT")
            except (httpx.HTTPError, ValueError, ServerLeaseLostError) as exc:
                with self._state_lock:
                    if self._stop.is_set():
                        return
                    if (
                        isinstance(exc, httpx.HTTPError)
                        and not self._lost.is_set()
                        and time.monotonic() < self._expires_at
                    ):
                        continue
                    self._lost.set()
                    if self._interrupt:
                        os.kill(os.getpid(), signal.SIGINT)
                return

    def close(self) -> None:
        with self._state_lock:
            self._stop.set()
        if self._thread.ident is not None:
            self._thread.join()
        try:
            self._request("DELETE")
        except httpx.HTTPError:
            pass
