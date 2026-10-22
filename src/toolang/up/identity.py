"""Claim full source ownership and allocate source-local roaming identities."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import psutil

from toolang.common.files import EditLock, atomic_write_text
from toolang.common.instance_ids import (
    area_id,
    decode_instance_id,
    roaming_instance_id,
    roaming_timestamp_ms,
)
from toolang.common import layout as paths
from toolang.common.layout import AgentLayout, derive_agent_name
from .instance import AGENT_INSTANCE_FORMAT, read_instance, update_instance
from .private_paths import verify_area, verify_home_paths, visiting_paths


def identity_bindings(layout: AgentLayout) -> dict:
    return {
        "root": str(layout.root),
        "home": str(layout.home),
        "store": str(layout.run_store),
        "name": layout.name,
        "placement": layout.placement,
        "instance_id": layout.instance_id,
        "instance_name": layout.instance_name,
        "runtime_instance": layout.instance,
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


def _canonical_path(value: object) -> Path:
    if not isinstance(value, str):
        raise ValueError("instance identity requires a canonical absolute path")
    path = Path(value)
    if not path.is_absolute() or str(path.resolve()) != value:
        raise ValueError("instance identity requires a canonical absolute path")
    return path


def layout_from_control(control: dict) -> AgentLayout:
    """Recompute an identity from its full bindings, never trust its short tag."""
    root = _canonical_path(control.get("toolang_root"))
    name = control.get("name")
    if not isinstance(name, str):
        raise ValueError("instance identity requires an agent name")
    placement = control.get("placement")
    if placement == "resident":
        layout = AgentLayout.resident(root, name)
    elif placement == "visiting":
        source = control.get("source_ref")
        if not isinstance(source, str) or not source:
            raise ValueError("visiting identity has no source URL")
        layout = AgentLayout.visiting(source, name, toolang_root=root)
        assert layout.instance_id is not None
        if layout.root != paths.VISITING_ROOT / area_id(str(root)) / layout.instance_id:
            raise ValueError("visiting instance identity follows an invalid root link")
    elif placement == "roaming":
        source_path = _canonical_path(control.get("source_path"))
        token = control.get("instance_id")
        if not isinstance(token, str):
            raise ValueError("roaming identity requires an instance ID")
        if roaming_instance_id(str(source_path), roaming_timestamp_ms(token)) != token:
            raise ValueError("roaming instance identity does not match its source path")
        layout = AgentLayout.roaming(source_path, instance_id=token, toolang_root=root)
        if layout.root != source_path.parent / ".toolang" / area_id(str(root)) / token:
            raise ValueError("roaming instance identity follows an invalid root link")
    else:
        raise ValueError("invalid instance identity placement")
    runtime_instance = control.get("runtime_instance")
    if runtime_instance is not None and not isinstance(runtime_instance, str):
        raise ValueError("invalid runtime instance in identity bindings")
    layout = replace(
        layout,
        instance=runtime_instance,
        project_inputs=control.get("project_inputs"),
        managed_membership=control.get("managed_membership"),
    )
    expected = identity_bindings(layout)
    inconsistent = [
        key
        for key, value in expected.items()
        if (
            key not in control
            or type(control[key]) is not type(value)
            or control[key] != value
        )
    ]
    if inconsistent:
        raise ValueError(
            f"inconsistent instance identity bindings: {', '.join(inconsistent)}"
        )
    return layout


def verify_identity(layout: AgentLayout, control: dict) -> None:
    """Require all bindings, including fields excluded from layout equality."""
    actual = identity_bindings(layout_from_control(control))
    if actual != identity_bindings(layout):
        raise ValueError("instance ID collision: conflicting source ownership")


@contextmanager
def visiting_home_guard(layout: AgentLayout) -> Iterator[None]:
    """Claim a new home or verify full ownership before any cache/history write."""
    if layout.placement != "visiting":
        raise ValueError("visiting home guard requires a visiting layout")
    expected = identity_bindings(layout)

    def verify() -> None:
        entry = read_instance(layout.runtime_status)
        if entry is None:
            raise ValueError("visiting instance ID collision or unbound home")
        verify_identity(layout, entry["control"])

    with visiting_paths(layout) as pinned:
        if not pinned.created:
            verify()
        with EditLock(layout.home_edit_lock).write():
            pinned.recheck()
            if pinned.created:

                def claim(entry):
                    entry.update(launch_id=uuid4().hex, control=expected, workload={})

                update_instance(layout.runtime_status, claim)
            else:
                verify()
            yield


def _retained_layout(root: Path, name: str) -> AgentLayout:
    if root.is_symlink():
        raise ValueError(f"invalid retained instance root: {root}")
    decode_instance_id(root.name, marker="s")
    statuses = list((root / "agents").glob("*/.runtime/status.json"))
    if len(statuses) > 1:
        raise ValueError(f"invalid retained instance identity: {root}")
    status = (
        statuses[0] if statuses else root / "agents" / name / ".runtime/status.json"
    )
    entry = read_instance(status)
    if entry is None:
        raise ValueError(
            f"invalid retained instance identity: {root}"
        ) from FileNotFoundError(status)
    layout = layout_from_control(entry["control"])
    if layout.placement != "roaming" or layout.root != root:
        raise ValueError(f"inconsistent retained instance identity: {root}")
    verify_home_paths(root, layout.name)
    assert layout.toolang_root is not None
    verify_area(root.parent, layout.toolang_root)
    return layout


def select_roaming(
    source: Path, instance_id: str | None = None, *, toolang_root: Path
) -> AgentLayout:
    """Resolve retained identity without materialization or global discovery."""
    source = source.expanduser().resolve()
    if instance_id is not None:
        decode_instance_id(instance_id, marker="s")
    guard = EditLock(source.parent / ".toolang" / ".state" / "launch.lock")
    return guard.read(
        lambda: _select_retained_roaming(source, instance_id, toolang_root)
    )


def retained_roaming(source: Path, *, toolang_root: Path) -> tuple[AgentLayout, ...]:
    source = source.expanduser().resolve()
    cache = (
        source.parent / ".toolang" / area_id(str(toolang_root.expanduser().resolve()))
    )
    if not cache.exists():
        return ()
    verify_area(cache, toolang_root)
    prefix = roaming_instance_id(str(source), 0)[:8]
    layouts = tuple(
        _retained_layout(root, derive_agent_name(source.stem))
        for root in sorted(cache.glob(prefix + "*"), reverse=True)
        if not (root.is_dir() and not root.is_symlink() and not any(root.iterdir()))
    )
    return tuple(layout for layout in layouts if layout.source_path == source)


def _select_retained_roaming(
    source: Path, instance_id: str | None, toolang_root: Path
) -> AgentLayout:
    cache = (
        source.parent / ".toolang" / area_id(str(toolang_root.expanduser().resolve()))
    )
    if instance_id is not None:
        verify_area(cache, toolang_root)
    roots = (
        [_retained_layout(cache / instance_id, derive_agent_name(source.stem))]
        if instance_id is not None
        else retained_roaming(source, toolang_root=toolang_root)
    )
    for layout in roots:
        if layout.source_path != source:
            if instance_id is not None:
                raise ValueError("instance belongs to a different source")
            continue
        return layout
    raise FileNotFoundError(f"no retained instance for {source}")


def allocate_roaming(source: Path, *, toolang_root: Path) -> AgentLayout:
    """Allocate once under source/registry guards; collisions fail without retry."""
    from .observation import RuntimeRegistration, registration_lock, verify_registration

    source = source.expanduser().resolve()
    toolang_root = toolang_root.expanduser().resolve()
    if source.suffix != ".too":
        raise ValueError(f"agent program must be a .too file: {source}")
    cache = source.parent / ".toolang" / area_id(str(toolang_root))
    with EditLock(source.parent / ".toolang" / ".state" / "launch.lock").write():
        verify_area(cache, toolang_root, create=True)
        highest = -1
        for previous in retained_roaming(source, toolang_root=toolang_root):
            assert previous.instance_id is not None
            highest = max(highest, roaming_timestamp_ms(previous.instance_id))
        timestamp_ms = max(time.time_ns() // 1_000_000, highest + 1)
        token = roaming_instance_id(str(source), timestamp_ms)
        while (
            (cache / token).is_dir()
            and not (cache / token).is_symlink()
            and not any((cache / token).iterdir())
        ):
            timestamp_ms += 1
            token = roaming_instance_id(str(source), timestamp_ms)
        layout = AgentLayout.roaming(
            source, instance_id=token, toolang_root=toolang_root
        )
        with registration_lock(toolang_root):
            if layout.root.exists() or layout.root.is_symlink():
                raise FileExistsError(
                    f"roaming instance ID collision: {layout.instance_name}"
                )
            verify_registration(layout, toolang_root=toolang_root, require_absent=True)
            # Exclusive creation cannot replace even an empty preexisting root.
            layout.root.mkdir(mode=0o700)
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
            atomic_write_text(layout.runtime_status, json.dumps(payload))
            try:
                RuntimeRegistration(
                    layout,
                    toolang_root=toolang_root,
                    launch_id=payload["instances"]["default"]["launch_id"],
                ).__enter__()
            except BaseException as exc:
                from .workload import fail_preparation

                fail_preparation(layout, exc)
                raise
        return layout
