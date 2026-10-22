"""Host registration publication, observation, and generation-fenced cleanup."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from toolang.common.files import atomic_write_text, file_write_lock
from toolang.common.layout import AgentLayout
from .instance import RUNTIME_STATUS_FORMAT, read_instance
from .records import InstanceIdentity


def instance_filename(layout: AgentLayout) -> str:
    return f"{layout.address}.json"


def read_registration(path: Path) -> dict[str, Any] | None:
    try:
        reference = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(reference, dict):
        raise ValueError("invalid runtime reference")
    if (
        type(reference.get("version")) is not int
        or reference["version"] != RUNTIME_STATUS_FORMAT
    ):
        raise ValueError("unsupported runtime reference version")
    if not isinstance(reference.get("status"), str) or not isinstance(
        reference.get("launch_id"), str
    ):
        raise ValueError("incomplete runtime reference")
    status = Path(reference["status"])
    if (
        not status.is_absolute()
        or str(status) != reference["status"]
        or ".." in status.parts
    ):
        raise ValueError("runtime reference requires a normalized absolute status path")
    return reference


def registration_current(
    path: Path, launch_id: str | None, status_path: Path | None = None
) -> bool:
    reference = read_registration(path)
    return (
        reference is not None
        and (launch_id is None or reference["launch_id"] == launch_id)
        and (status_path is None or reference["status"] == str(status_path))
    )


def _register_lock(path: Path) -> Path:
    return path.parent / "register.lock"


@contextmanager
def registration_lock(toolang_root: Path) -> Iterator[None]:
    """Serialize candidate checks and publication across source-local allocators."""
    with file_write_lock(toolang_root / ".runtime" / "active" / "register.lock"):
        yield


def verify_registration(
    layout: AgentLayout, *, toolang_root: Path, require_absent: bool = False
) -> None:
    """Reject an occupied name before attempting this caller's publication."""
    path = toolang_root / ".runtime" / "active" / instance_filename(layout)
    reference = read_registration(path)
    if reference is not None and (
        require_absent or reference["status"] != str(layout.runtime_status)
    ):
        raise ValueError(f"place code collision in runtime registry: {layout.address}")


class RuntimeRegistration:
    """Publish only a discovery reference to an already initialized workload."""

    def __init__(
        self,
        layout: AgentLayout,
        *,
        toolang_root: Path,
        launch_id: str,
    ) -> None:
        self.token = launch_id
        self.toolang_root = toolang_root.expanduser().resolve()
        self.layout = replace(layout, root=self.toolang_root)
        self.path = (
            self.toolang_root / ".runtime" / "active" / instance_filename(layout)
        )
        self.payload = {
            "version": RUNTIME_STATUS_FORMAT,
            "status": str(layout.runtime_status),
            "launch_id": self.token,
        }

    def publish(self) -> RuntimeRegistration:
        """Publish this initialized generation under the shared registration lock."""
        with registration_lock(self.toolang_root):
            verify_registration(self.layout, toolang_root=self.toolang_root)
            entry = read_instance(self.layout.runtime_status, self.layout.instance)
            if entry is None or entry["launch_id"] != self.token:
                raise ValueError(
                    "runtime publication requires matching instance identity"
                )
            InstanceIdentity.from_data(entry["control"]).verify(self.layout)
            try:
                atomic_write_text(self.path, json.dumps(self.payload))
            except BaseException:
                self.close()
                raise
        return self

    def __enter__(self) -> RuntimeRegistration:
        return self.publish()

    def close(self) -> None:
        _remove_reference(self.path, self.token, self.layout.runtime_status)

    def __exit__(self, *exc: object) -> None:
        self.close()


def _remove_reference(path: Path, launch_id: str, status_path: Path) -> None:
    try:
        if not path.is_file():
            return
        with file_write_lock(_register_lock(path)):
            reference = read_registration(path)
            if (
                reference is not None
                and reference["launch_id"] == launch_id
                and reference["status"] == str(status_path)
            ):
                path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def remove_registration(layout: AgentLayout, launch_id: str) -> None:
    entry = read_instance(layout.runtime_status, layout.instance)
    if entry is None or entry["launch_id"] != launch_id:
        return
    root = entry["control"].get("root")
    if isinstance(root, str):
        _remove_reference(
            Path(root)
            / ".runtime"
            / "active"
            / instance_filename(
                InstanceIdentity.from_data(entry["control"]).to_layout()
            ),
            launch_id,
            layout.runtime_status,
        )
