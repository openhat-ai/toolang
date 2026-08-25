"""Persisted AgentServer hosting records."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from toolang.base.types.sandbox import SandboxRef
from toolang.common.files import atomic_write_text, file_write_lock

SANDBOX_STATE_VERSION = 1


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
            if not path.is_file():
                return None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
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
