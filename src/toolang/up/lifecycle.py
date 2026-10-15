"""Server-owned retention and admission for one launch."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import threading
import time

from .types import ServerLifetime

LEASE_RENEW_SEC = 5.0
LEASE_TTL_SEC = 15.0
STARTUP_HOLD_SEC = 30.0
LAUNCH_HEADER = "X-Toolang-Launch-ID"


class LifecycleConflict(ValueError):
    """A request targets a closing or replaced execution owner."""


class ServerLifecycle:
    """Coordinate short admissions, client leases, and idle exit election."""

    def __init__(
        self,
        launch_id: str,
        lifetime: ServerLifetime,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_promote: Callable[[], None] | None = None,
    ) -> None:
        self.launch_id = launch_id
        self.lifetime = lifetime
        self.closing = False
        self._clock = clock
        self._lock = threading.RLock()
        self._leases: dict[str, float] = {}
        self._startup_until: float | None = None
        self._admissions = 0
        self._on_promote = on_promote

    def ready(self) -> None:
        with self._lock:
            self._startup_until = self._clock() + STARTUP_HOLD_SEC

    def _check(self, launch_id: str | None = None) -> None:
        if launch_id is not None and launch_id != self.launch_id:
            raise LifecycleConflict("server launch changed; acquire it again")
        if self.closing:
            raise LifecycleConflict("server is closing; wait for it to stop")

    @contextmanager
    def admit(self, launch_id: str | None = None) -> Iterator[None]:
        with self._lock:
            self._check(launch_id)
            self._admissions += 1
        try:
            yield
        finally:
            with self._lock:
                self._admissions -= 1

    def lease(self, launch_id: str, client_id: str) -> None:
        with self._lock:
            self._check(launch_id)
            if self.lifetime == "automatic":
                self._leases[client_id] = self._clock() + LEASE_TTL_SEC
                self._startup_until = 0.0

    def release(self, launch_id: str, client_id: str) -> None:
        with self._lock:
            if launch_id == self.launch_id:
                self._leases.pop(client_id, None)

    def promote(self, launch_id: str) -> None:
        with self._lock:
            self._check(launch_id)
            if self.lifetime == "foreground":
                raise LifecycleConflict("start cannot detach foreground serve")
            self.lifetime = "persistent"
            self._leases.clear()
        if self._on_promote is not None:
            self._on_promote()

    def elect_exit(self, has_work: Callable[[], bool]) -> bool:
        with self._lock:
            if self.lifetime != "automatic" or self.closing:
                return False
            now = self._clock()
            self._leases = {k: v for k, v in self._leases.items() if v > now}
            if (
                self._startup_until is None
                or self._startup_until > now
                or self._leases
                or self._admissions
                or has_work()
            ):
                return False
            self.closing = True
            return True
