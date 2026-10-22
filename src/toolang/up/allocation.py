"""Allocate and select source-local roaming identities."""

from pathlib import Path
import time
from uuid import uuid4

from toolang.common.files import EditLock
from toolang.common.layout import AgentLayout, derive_agent_name
from toolang.common.place_codes import (
    decode_place_code,
    root_code,
    roaming_place_code,
    roaming_timestamp_ms,
)
from .identity import layout_from_control
from .instance import control_owner, read_instance
from .private_paths import verify_home_paths, verify_root_namespace
from .registry import RuntimeRegistration, registration_lock, verify_registration
from .workload import fail_preparation, prepare_workload


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
            launch_id = uuid4().hex
            prepare_workload(
                layout,
                launch_id=launch_id,
                requested_at=time.time(),
                owner=control_owner(),
            )
            try:
                RuntimeRegistration(
                    layout,
                    toolang_root=toolang_root,
                    launch_id=launch_id,
                ).publish()
            except BaseException as exc:
                fail_preparation(layout, exc)
                raise
        return layout
