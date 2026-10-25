"""Process-local lease tokens shared by backend and HTTP messaging."""

from functools import lru_cache
import os
from uuid import uuid4


@lru_cache
def _process_token(pid: int) -> str:
    return str(uuid4())


def host_token() -> str:
    """Share a lease across the resident lifecycle and its message tools."""
    return _process_token(os.getpid())
