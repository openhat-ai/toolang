"""Persisted AgentServer hosting records."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from toolang.base.types.sandbox import SandboxRef
from toolang.common.files import atomic_write_text, file_write_lock
from toolang.teaming.schemas import HubConnection

SANDBOX_STATE_VERSION = 1


class HubRecord(BaseModel):
    """Private discovery and process identity for a root's local Hub."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    pid: int = Field(gt=0)
    created: float = Field(gt=0)
    port: int = Field(ge=1, le=65535)
    token: str = Field(min_length=32)
    human: str
    identity: str
    status: Literal["starting", "running"] = "running"

    @property
    def connection(self) -> HubConnection:
        return HubConnection(
            f"http://127.0.0.1:{self.port}", self.token, self.human, self.identity
        )

    def save(self, path: Path) -> None:
        if path.exists():
            path.chmod(0o600)
        atomic_write_text(path, self.model_dump_json() + "\n")

    @classmethod
    def load(cls, path: Path) -> HubRecord | None:
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ValueError(f"Invalid Hub record: {path}") from exc


@dataclass(frozen=True, slots=True)
class SandboxState:
    """Persisted control-side reference to one sandboxed AgentServer workload."""

    sandbox: str
    ref: SandboxRef

    def __post_init__(self) -> None:
        sandbox = self.sandbox.strip()
        if not sandbox:
            raise ValueError("sandbox state requires sandbox")
        object.__setattr__(self, "sandbox", sandbox)

    def save(self, path: Path) -> None:
        payload = {
            "version": SANDBOX_STATE_VERSION,
            "sandbox": self.sandbox,
            "ref": self.ref.to_data(),
        }
        with file_write_lock(path.with_suffix(".lock")):
            atomic_write_text(
                path,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
            )

    @classmethod
    def load(cls, path: Path) -> SandboxState | None:
        with file_write_lock(path.with_suffix(".lock")):
            return cls.snapshot(path)

    @classmethod
    def snapshot(cls, path: Path) -> SandboxState | None:
        """Read an atomic reference without waiting on the launcher lock."""

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid sandbox state: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"invalid sandbox state: {path}")
        version = payload.get("version")
        if version != SANDBOX_STATE_VERSION:
            raise ValueError(f"unsupported sandbox state version: {path}")
        sandbox = payload.get("sandbox")
        if not isinstance(sandbox, str) or not sandbox.strip():
            raise ValueError(f"sandbox state is missing sandbox: {path}")
        return cls(
            sandbox=sandbox.strip(),
            ref=SandboxRef.from_data(payload.get("ref")),
        )
