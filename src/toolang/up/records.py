"""Passive discovery records shared by host and mapped workload views."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

RUNTIME_INFO_FORMAT = 6


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str):
        raise ValueError("runtime reference requires a normalized absolute path")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise ValueError("runtime reference requires a normalized absolute path")
    return path


@dataclass(frozen=True, slots=True)
class RuntimeReference:
    """One home and launch, expressed in the reader's filesystem view."""

    status: Path
    launch_id: str
    location: str = "host"
    sandbox: str = "host"
    source: Path | None = None
    source_ref: str | None = None
    address: str | None = None

    @property
    def home(self) -> Path:
        return self.status.parent.parent

    @classmethod
    def from_data(cls, value: object) -> RuntimeReference:
        if not isinstance(value, dict):
            raise ValueError("invalid runtime reference")
        payload = cast(dict[str, object], value)
        version = payload.get("version")
        if type(version) is not int:
            raise ValueError("unsupported runtime reference version")
        if version == 5:
            status = _absolute_path(payload.get("status"))
            launch = payload.get("launch_id")
            if not isinstance(launch, str) or not launch:
                raise ValueError("incomplete runtime reference")
            return cls(status, launch)
        if version != RUNTIME_INFO_FORMAT:
            raise ValueError("unsupported runtime reference version")
        home = _absolute_path(payload.get("home"))
        launch = payload.get("launch")
        location = payload.get("location")
        sandbox = payload.get("sandbox")
        if (
            not isinstance(launch, str)
            or not launch
            or not isinstance(location, str)
            or location not in {"host", "guest"}
            or not isinstance(sandbox, str)
            or not sandbox
        ):
            raise ValueError("incomplete runtime reference")
        source = payload.get("source")
        source_ref = payload.get("source_ref")
        address = payload.get("address")
        if address is not None and (not isinstance(address, str) or not address):
            raise ValueError("invalid runtime address")
        if source_ref is not None and (
            not isinstance(source_ref, str) or not source_ref
        ):
            raise ValueError("invalid runtime source reference")
        return cls(
            home / ".runtime" / "status.json",
            launch,
            location,
            sandbox,
            _absolute_path(source) if source is not None else None,
            source_ref,
            address,
        )

    def to_data(self) -> dict[str, object]:
        return {
            "version": RUNTIME_INFO_FORMAT,
            "home": str(self.home),
            "launch": self.launch_id,
            "location": self.location,
            "sandbox": self.sandbox,
            **({"address": self.address} if self.address is not None else {}),
            **({"source": str(self.source)} if self.source is not None else {}),
            **({"source_ref": self.source_ref} if self.source_ref is not None else {}),
        }
