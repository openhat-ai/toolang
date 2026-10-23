"""Read current local Hub discovery without coupling clients to process control."""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
import os
from pathlib import Path
from uuid import uuid4

from .errors import BackendUnavailable
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from toolang.common.files import atomic_write_text
from toolang.common.urls import replace_guest_loopback
from .schemas import HubConnection


class HubDiscovery(BaseModel):
    """Private discovery and process identity for a root's local Hub."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    pid: int = Field(gt=0)
    created: float = Field(gt=0)
    port: int = Field(ge=1, le=65535)
    human: str
    identity: str
    status: Literal["starting", "running"] = "running"

    @property
    def connection(self) -> HubConnection:
        return HubConnection(f"http://127.0.0.1:{self.port}", self.human, self.identity)

    def save(self, path: Path) -> None:
        if path.exists():
            path.chmod(0o600)
        atomic_write_text(path, self.model_dump_json() + "\n")

    @classmethod
    def load(cls, path: Path) -> HubDiscovery | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.model_validate(data)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ValueError(f"Invalid Hub discovery: {path}") from exc


def hub_connection(root: Path, *, gateway: str | None = None) -> HubConnection:
    try:
        record = HubDiscovery.load(root / "hub" / "hub.json")
    except ValueError as exc:
        raise BackendUnavailable("Hub discovery is invalid; check 'too hub'") from exc
    if record is None or record.status != "running":
        raise BackendUnavailable("Hub is unavailable; run 'too hub start'")
    connection = record.connection
    return replace(
        connection, endpoint=replace_guest_loopback(connection.endpoint, gateway)
    )


@lru_cache
def _process_token(pid: int) -> str:
    return str(uuid4())


def host_token() -> str:
    """Share a lease across the resident lifecycle and its message tools."""
    return _process_token(os.getpid())
