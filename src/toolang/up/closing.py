"""A process-owned deadline independent of asynchronous workload cancellation."""

from __future__ import annotations

import os
import math
from collections.abc import Callable
import threading
import time
import psutil

FINALIZATION_GRACE_SEC = 10.0
OWNER_CHANNEL_REASON = "foreground owner channel closed"


class ClosingDeadline:
    """Start once; a stuck event loop cannot indefinitely retain a foreground owner."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        on_begin: Callable[[float, str], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.started: float | None = None
        self.reason: str | None = None
        self._timer: threading.Timer | None = None
        self._on_begin = on_begin

    def begin(self, reason: str, *, started_at: float | None = None) -> None:
        if self.started is not None:
            return
        now = time.time()
        self.started = (
            min(now, started_at)
            if started_at is not None and math.isfinite(started_at)
            else now
        )
        self.reason = reason
        if self.enabled:
            self._timer = threading.Timer(
                max(0, self.started + FINALIZATION_GRACE_SEC - now), self._expire
            )
            self._timer.daemon = True
            self._timer.start()
        if self._on_begin is not None:
            self._on_begin(self.started, reason)

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def _expire(self) -> None:
        try:
            for child in reversed(psutil.Process().children(recursive=True)):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            os.set_blocking(2, False)
            os.write(
                2,
                b"Foreground finalization deadline exceeded; records may be incomplete.\n",
            )
        except (OSError, psutil.Error):
            pass
        finally:
            os._exit(124)
