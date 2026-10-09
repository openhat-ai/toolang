"""Read current local Hub discovery without coupling clients to process control."""

from functools import lru_cache
import os
from pathlib import Path
from uuid import uuid4

from .errors import BackendUnavailable
from .records import HubRecord
from .schemas import HubConnection


def hub_connection(root: Path) -> HubConnection:
    try:
        record = HubRecord.load(root / ".runtime" / "hub.json")
    except ValueError as exc:
        raise BackendUnavailable(
            "Hub discovery is invalid; check 'too hub status'"
        ) from exc
    if record is None or record.status != "running":
        raise BackendUnavailable("Hub is unavailable; run 'too hub start'")
    return record.connection


@lru_cache
def _process_token(pid: int) -> str:
    return str(uuid4())


def host_token() -> str:
    """Share a lease across the resident lifecycle and its message tools."""
    return _process_token(os.getpid())
