"""Host-owned instance discovery, independent of messaging and driver selection."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import psutil

from toolang.common.files import atomic_write_text
from toolang.common.layout import AgentLayout
from toolang.up.types import ExecutionDrivers


class RuntimeRegistration:
    """Publish a running instance into explicitly selected observation scopes."""

    def __init__(
        self,
        layout: AgentLayout,
        *,
        roots: tuple[Path, ...],
        drivers: ExecutionDrivers,
        endpoint: str = "",
        sandbox_runtime: str | None = None,
    ) -> None:
        self.instance = hashlib.sha256(str(layout.run_store).encode()).hexdigest()[:16]
        self.token = uuid4().hex
        self.paths = tuple(
            root / ".runtime" / "instances" / f"{self.instance}.json"
            for root in dict.fromkeys(roots)
        )
        self.payload = {
            "instance": self.instance,
            "agent": f"agent:{layout.name}"
            if layout.placement == "resident" and layout.instance is None
            else f"agent:{layout.name}@{self.instance[:8]}",
            "placement": layout.placement,
            "home": str(layout.home),
            "store": str(layout.run_store),
            "drivers": sorted(drivers),
            "endpoint": endpoint,
            "pid": os.getpid(),
            "created": psutil.Process().create_time(),
            "token": self.token,
            "sandbox_runtime": sandbox_runtime,
            "root": str(layout.root),
            "name": layout.name,
            "runtime_instance": layout.instance,
        }

    def __enter__(self) -> RuntimeRegistration:
        try:
            for path in self.paths:
                payload = dict(self.payload)
                if (
                    path.parents[2] != Path(payload["root"])
                    and "@" not in payload["agent"]
                ):
                    payload["agent"] = f"{payload['agent']}@{self.instance[:8]}"
                atomic_write_text(path, json.dumps(payload))
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        for path in self.paths:
            try:
                if json.loads(path.read_text()).get("token") == self.token:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass

    def __exit__(self, *exc: object) -> None:
        self.close()


def running_instances(root: Path) -> tuple[dict, ...]:
    """Read live registrations, rejecting stale PID reuse without altering files."""
    instances = []
    for path in sorted((root / ".runtime" / "instances").glob("*.json")):
        try:
            payload = json.loads(path.read_text())
            if not isinstance(payload, dict):
                continue
            if not isinstance(payload.get("drivers"), list) or any(
                driver not in {"chat", "talk", "chore"} for driver in payload["drivers"]
            ):
                continue
            if not isinstance(payload.get("instance"), str):
                continue
            if payload.get("sandbox_runtime") is not None:
                from toolang.up.process import AgentProcess

                layout = AgentLayout(
                    Path(payload["root"]),
                    payload["name"],
                    payload["placement"],
                    instance=payload["runtime_instance"],
                )
                process_owner = AgentProcess(layout)
                reference = process_owner.reference
                if (
                    reference is None
                    or reference.ref.runtime_id != payload["sandbox_runtime"]
                ):
                    continue
                status = process_owner.status(ui_base_url="", check_health=False)
                if status is None or status.status != "running":
                    continue
            else:
                process = psutil.Process(payload["pid"])
                if (
                    process.create_time() != payload["created"]
                    or process.status() == psutil.STATUS_ZOMBIE
                ):
                    continue
            if not isinstance(payload["store"], str) or not isinstance(
                payload["agent"], str
            ):
                continue
            instances.append(payload)
        except (OSError, ValueError, KeyError, TypeError, psutil.Error):
            continue
    return tuple(instances)
