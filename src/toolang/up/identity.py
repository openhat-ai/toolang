"""Validate immutable instance identities and claim their homes."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import uuid4

from toolang.common.files import EditLock
from toolang.common.place_codes import (
    root_code,
    roaming_place_code,
    roaming_timestamp_ms,
)
from toolang.common import layout as paths
from toolang.common.layout import AgentLayout, AgentPlacement
from .instance import read_instance, update_instance
from .private_paths import verify_root_namespace, verify_home_paths, visiting_paths


@dataclass(frozen=True, slots=True)
class InstanceIdentity:
    """Immutable identity bindings, separate from mutable runtime metadata."""

    place: str
    home: str
    store: str
    name: str
    placement: AgentPlacement
    place_code: str | None
    address: str
    runtime_instance: str | None
    source_id: str | None
    source_path: str | None
    source_ref: str | None
    request_order_ns: int | None
    source_edit_lock: str
    root: str | None
    project_inputs: bool
    managed_membership: bool

    @classmethod
    def from_layout(cls, layout: AgentLayout) -> InstanceIdentity:
        return cls(
            place=str(layout.place),
            home=str(layout.home),
            store=str(layout.run_store),
            name=layout.name,
            placement=layout.placement,
            place_code=layout.place_code,
            address=layout.address,
            runtime_instance=layout.instance,
            source_id=layout.source_id,
            source_path=str(layout.source_path)
            if layout.source_path is not None
            else None,
            source_ref=layout.source_ref,
            request_order_ns=layout.request_order_ns,
            source_edit_lock=str(layout.place_edit_lock),
            root=str(layout.root) if layout.root is not None else None,
            project_inputs=bool(layout.project_inputs),
            managed_membership=bool(layout.managed_membership),
        )

    @classmethod
    def from_data(cls, control: Mapping[str, object]) -> InstanceIdentity:
        """Validate every required binding against its reconstructed identity."""
        return cls._parse(control)[0]

    @classmethod
    def _parse(
        cls, control: Mapping[str, object]
    ) -> tuple[InstanceIdentity, AgentLayout]:
        layout = _identity_layout(control)
        identity = cls.from_layout(layout)
        inconsistent = [
            key
            for key, value in identity.to_data().items()
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
        return identity, layout

    def to_data(self) -> dict[str, object]:
        return asdict(self)

    def to_layout(self) -> AgentLayout:
        return self._parse(self.to_data())[1]


def identity_bindings(layout: AgentLayout) -> dict[str, object]:
    return InstanceIdentity.from_layout(layout).to_data()


def _canonical_path(value: object) -> Path:
    if not isinstance(value, str):
        raise ValueError("instance identity requires a canonical absolute path")
    path = Path(value)
    if not path.is_absolute() or str(path.resolve()) != value:
        raise ValueError("instance identity requires a canonical absolute path")
    return path


def _identity_layout(control: Mapping[str, object]) -> AgentLayout:
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
    return layout


def layout_from_control(control: Mapping[str, object]) -> AgentLayout:
    return InstanceIdentity._parse(control)[1]


def verify_identity(layout: AgentLayout, control: Mapping[str, object]) -> None:
    """Require all bindings, including fields excluded from layout equality."""
    if InstanceIdentity.from_data(control) != InstanceIdentity.from_layout(layout):
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
