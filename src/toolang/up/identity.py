"""Allocate and select durable, source-local roaming instances."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from toolang.common.files import atomic_write_text, file_write_lock
from toolang.common.layout import AgentLayout, derive_agent_name
from .instance import AGENT_INSTANCE_FORMAT, read_instance, patch_workload
import os
import psutil

_ROAMING_ID = re.compile(r"r[0-9a-f]{32}\Z")


def select_roaming(source: Path, instance_id: str | None = None) -> AgentLayout:
    """Resolve retained identity without materialization or global discovery."""
    source = source.expanduser().resolve()
    if instance_id is not None and not _ROAMING_ID.fullmatch(instance_id):
        raise ValueError("--instance requires r followed by 32 lowercase hex digits")
    name = derive_agent_name(source.stem)
    roots = (
        [source.parent / ".toolang" / f"{name}@{instance_id}"]
        if instance_id
        else sorted((source.parent / ".toolang").glob(f"{name}@r*"), reverse=True)
    )
    for root in roots:
        token = root.name.partition("@")[2]
        if not _ROAMING_ID.fullmatch(token):
            raise ValueError(f"invalid instance identity: {root}")
        layout = AgentLayout.roaming(source, instance_id=token)
        entry = read_instance(layout.runtime_status)
        if entry is None or entry["control"].get("instance_id") != token:
            raise ValueError(f"invalid retained instance: {root}")
        if entry["control"].get("source_path") != str(source):
            if instance_id is not None:
                raise ValueError("instance belongs to a different source")
            continue
        _validate_retained_identity(layout, entry["control"])
        return layout
    raise FileNotFoundError(f"no retained instance for {source}")


def allocate_roaming(source: Path, *, toolang_root: Path) -> AgentLayout:
    """Publish initial status atomically before expensive source preparation."""
    source = source.expanduser().resolve()
    if source.suffix != ".too":
        raise ValueError(f"agent program must be a .too file: {source}")
    cache = source.parent / ".toolang"
    with file_write_lock(cache / ".state" / "launch.lock"):
        highest = 0
        for root in cache.glob("*@r*"):
            token = root.name.partition("@")[2]
            if _ROAMING_ID.fullmatch(token):
                entry = read_instance(
                    root
                    / "agents"
                    / root.name.partition("@")[0]
                    / ".runtime"
                    / "status.json"
                )
                if entry is None or entry["control"].get("instance_id") != token:
                    raise ValueError(f"invalid retained instance: {root}")
                recorded_source = entry["control"].get("source_path")
                if (
                    not isinstance(recorded_source, str)
                    or not Path(recorded_source).is_absolute()
                ):
                    raise ValueError(f"invalid retained source identity: {root}")
                previous = AgentLayout.roaming(Path(recorded_source), instance_id=token)
                if previous.root != root.resolve():
                    raise ValueError(f"invalid retained root identity: {root}")
                _validate_retained_identity(previous, entry["control"])
                highest = max(highest, int(token[1:17], 16))
        order = max(time.time_ns(), highest + 1)
        token = (
            f"r{order:016x}"
            + sha256(str(source).encode() + b"\0" + uuid4().bytes).hexdigest()[:16]
        )
        layout = replace(
            AgentLayout.roaming(source, instance_id=token), toolang_root=toolang_root
        )
        stage = cache / f".staging-{uuid4().hex}"
        staged = replace(layout, root=stage)
        payload = {
            "version": AGENT_INSTANCE_FORMAT,
            "instances": {
                "default": {
                    "launch_id": uuid4().hex,
                    "control": identity_bindings(layout),
                    "workload": {
                        "status": "preparing",
                        "requested_at": time.time(),
                        "pid": os.getpid(),
                        "process_created": psutil.Process().create_time(),
                    },
                }
            },
        }
        atomic_write_text(staged.runtime_status, json.dumps(payload))
        stage.rename(layout.root)
        from .observation import RuntimeRegistration

        RuntimeRegistration(
            layout,
            toolang_root=toolang_root,
            drivers=frozenset(),
            launch_id=payload["instances"]["default"]["launch_id"],
        ).__enter__()
        return layout


def _validate_retained_identity(layout: AgentLayout, control: dict) -> None:
    expected = identity_bindings(layout)
    if any(
        control.get(key) != value
        for key, value in expected.items()
        if key != "toolang_root"
    ):
        raise ValueError(f"inconsistent retained instance identity: {layout.root}")


def identity_bindings(layout: AgentLayout) -> dict:
    return {
        "root": str(layout.root),
        "home": str(layout.home),
        "store": str(layout.run_store),
        "name": layout.name,
        "placement": layout.placement,
        "instance_id": layout.instance_id,
        "instance_name": layout.instance_name,
        "runtime_instance": None,
        "source_id": layout.source_id,
        "source_path": str(layout.source_path)
        if layout.source_path is not None
        else None,
        "source_ref": layout.source_ref,
        "request_order_ns": layout.request_order_ns,
        "source_edit_lock": str(layout.root_edit_lock),
        "toolang_root": str(layout.toolang_root)
        if layout.toolang_root is not None
        else None,
        "project_inputs": bool(layout.project_inputs),
        "managed_membership": bool(layout.managed_membership),
    }


def finish_preparation(layout: AgentLayout, message: str) -> None:
    """Finalize an owned command that returned without starting its workload."""
    entry = read_instance(layout.runtime_status)
    if entry is not None and entry["workload"].get("status") in {
        "preparing",
        "starting",
    }:
        fail_preparation(layout, RuntimeError(message))


def fail_preparation(layout: AgentLayout, error: BaseException) -> None:
    """Retain failed published identities without leaving an active reference."""
    from .observation import remove_registration

    entry = read_instance(layout.runtime_status)
    if entry is not None and not entry["control"].get("ref"):
        patch_workload(
            layout.runtime_status,
            entry["launch_id"],
            {"status": "failed", "pid": None, "message": str(error)},
        )
        remove_registration(layout, entry["launch_id"])
