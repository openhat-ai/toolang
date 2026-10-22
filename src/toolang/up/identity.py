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
from toolang.common.place_codes import (
    root_code,
    decode_place_code,
    roaming_place_code,
    roaming_timestamp_ms,
)
from toolang.common import layout as paths
from toolang.common.layout import AgentLayout, derive_agent_name
from .instance import RUNTIME_STATUS_FORMAT, read_instance, update_instance
from .private_paths import verify_root_namespace, verify_home_paths, visiting_paths


def identity_bindings(layout: AgentLayout) -> dict:
    return {
        "place": str(layout.place),
        "home": str(layout.home),
        "store": str(layout.run_store),
        "name": layout.name,
        "placement": layout.placement,
        "place_code": layout.place_code,
        "address": layout.address,
        "runtime_instance": layout.instance,
        "source_id": layout.source_id,
        "source_path": str(layout.source_path)
        if layout.source_path is not None
        else None,
        "source_ref": layout.source_ref,
        "request_order_ns": layout.request_order_ns,
        "source_edit_lock": str(layout.place_edit_lock),
        "root": str(layout.root) if layout.root is not None else None,
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
    root = _canonical_path(control.get("root"))
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
        layout = AgentLayout.visiting(source, name, root=root)
        assert layout.place_code is not None
        if (
            layout.place
            != paths.VISITING_BASE / root_code(str(root)) / layout.place_code
        ):
            raise ValueError(
                "visiting instance identity follows an invalid agent root link"
            )
    elif placement == "roaming":
        source_path = _canonical_path(control.get("source_path"))
        token = control.get("place_code")
        if not isinstance(token, str):
            raise ValueError("roaming identity requires a place code")
        if roaming_place_code(str(source_path), roaming_timestamp_ms(token)) != token:
            raise ValueError("roaming instance identity does not match its source path")
        layout = AgentLayout.roaming(source_path, place_code=token, root=root)
        if (
            layout.place
            != source_path.parent / ".toolang" / root_code(str(root)) / token
        ):
            raise ValueError(
                "roaming instance identity follows an invalid agent root link"
            )
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
        raise ValueError("place code collision: conflicting source ownership")


def read_home_identity(layout: AgentLayout, *, toolang_root: Path) -> dict | None:
    """Read and verify the ownership snapshot a home operation will pin."""
    if layout.placement != "resident":
        if layout.root != toolang_root.expanduser().resolve():
            raise ValueError("instance belongs to a different Toolang root")
        verify_root_namespace(layout.place.parent, toolang_root)
        verify_home_paths(layout.place, layout.name)
    entry = read_instance(layout.runtime_status, layout.instance)
    if layout.placement != "resident":
        if entry is None:
            raise FileNotFoundError(f"instance ownership not found: {layout.address}")
        verify_identity(layout, entry["control"])
    return entry


@contextmanager
def visiting_home_guard(layout: AgentLayout) -> Iterator[None]:
    """Claim a new home or verify full ownership before any cache/history write."""
    if layout.placement != "visiting":
        raise ValueError("visiting home guard requires a visiting layout")
    expected = identity_bindings(layout)

    def verify() -> None:
        entry = read_instance(layout.runtime_status)
        if entry is None:
            raise ValueError("visiting place code collision or unbound home")
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


def _retained_layout(place: Path, name: str) -> AgentLayout:
    if place.is_symlink():
        raise ValueError(f"invalid retained agent root: {place}")
    decode_place_code(place.name, marker="s")
    statuses = list((place / "agents").glob("*/.runtime/status.json"))
    if len(statuses) > 1:
        raise ValueError(f"invalid retained instance identity: {place}")
    status = (
        statuses[0] if statuses else place / "agents" / name / ".runtime/status.json"
    )
    entry = read_instance(status)
    if entry is None:
        raise ValueError(
            f"invalid retained instance identity: {place}"
        ) from FileNotFoundError(status)
    layout = layout_from_control(entry["control"])
    if layout.placement != "roaming" or layout.place != place:
        raise ValueError(f"inconsistent retained instance identity: {place}")
    verify_home_paths(place, layout.name)
    assert layout.root is not None
    verify_root_namespace(place.parent, layout.root)
    return layout


def select_roaming(
    source: Path, place_code: str | None = None, *, toolang_root: Path
) -> AgentLayout:
    """Resolve retained identity without materialization or global discovery."""
    source = source.expanduser().resolve()
    if place_code is not None:
        decode_place_code(place_code, marker="s")
    guard = EditLock(source.parent / ".toolang" / ".state" / "launch.lock")
    return guard.read(
        lambda: _select_retained_roaming(source, place_code, toolang_root)
    )


def retained_roaming(source: Path, *, toolang_root: Path) -> tuple[AgentLayout, ...]:
    source = source.expanduser().resolve()
    cache = (
        source.parent / ".toolang" / root_code(str(toolang_root.expanduser().resolve()))
    )
    if not cache.exists():
        return ()
    verify_root_namespace(cache, toolang_root)
    prefix = roaming_place_code(str(source), 0)[:8]
    layouts = tuple(
        _retained_layout(place, derive_agent_name(source.stem))
        for place in sorted(cache.glob(prefix + "*"), reverse=True)
        if not (place.is_dir() and not place.is_symlink() and not any(place.iterdir()))
    )
    return tuple(layout for layout in layouts if layout.source_path == source)


def _select_retained_roaming(
    source: Path, place_code: str | None, toolang_root: Path
) -> AgentLayout:
    cache = (
        source.parent / ".toolang" / root_code(str(toolang_root.expanduser().resolve()))
    )
    if place_code is not None:
        verify_root_namespace(cache, toolang_root)
    places = (
        [_retained_layout(cache / place_code, derive_agent_name(source.stem))]
        if place_code is not None
        else retained_roaming(source, toolang_root=toolang_root)
    )
    for layout in places:
        if layout.source_path != source:
            if place_code is not None:
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
    cache = source.parent / ".toolang" / root_code(str(toolang_root))
    with EditLock(source.parent / ".toolang" / ".state" / "launch.lock").write():
        verify_root_namespace(cache, toolang_root, create=True)
        highest = -1
        for previous in retained_roaming(source, toolang_root=toolang_root):
            assert previous.place_code is not None
            highest = max(highest, roaming_timestamp_ms(previous.place_code))
        timestamp_ms = max(time.time_ns() // 1_000_000, highest + 1)
        token = roaming_place_code(str(source), timestamp_ms)
        while (
            (cache / token).is_dir()
            and not (cache / token).is_symlink()
            and not any((cache / token).iterdir())
        ):
            timestamp_ms += 1
            token = roaming_place_code(str(source), timestamp_ms)
        layout = AgentLayout.roaming(source, place_code=token, root=toolang_root)
        with registration_lock(toolang_root):
            if layout.place.exists() or layout.place.is_symlink():
                raise FileExistsError(f"roaming place code collision: {layout.address}")
            verify_registration(layout, toolang_root=toolang_root, require_absent=True)
            # Exclusive creation cannot replace even an empty preexisting place.
            layout.place.mkdir(mode=0o700)
            payload = {
                "version": RUNTIME_STATUS_FORMAT,
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
