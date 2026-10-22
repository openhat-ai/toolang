"""Typed persisted identity bindings, separate from mutable runtime metadata."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from toolang.common import layout as paths
from toolang.common.layout import AgentLayout, AgentPlacement
from toolang.common.place_codes import (
    root_code,
    roaming_place_code,
    roaming_timestamp_ms,
)


@dataclass(frozen=True, slots=True)
class InstanceIdentity:
    """The complete immutable identity stored alongside runtime control fields."""

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

    def to_data(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_data(cls, control: Mapping[str, Any]) -> InstanceIdentity:
        """Recompute all bindings and reject missing, mistyped, or conflicting data."""
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
            if (
                roaming_place_code(str(source_path), roaming_timestamp_ms(token))
                != token
            ):
                raise ValueError(
                    "roaming instance identity does not match its source path"
                )
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
        expected = cls.from_layout(layout)
        inconsistent = [
            key
            for key, value in expected.to_data().items()
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
        return expected

    def to_layout(self) -> AgentLayout:
        return AgentLayout(
            place=Path(self.place),
            name=self.name,
            placement=self.placement,
            instance=self.runtime_instance,
            root=Path(self.root) if self.root is not None else None,
            source_id=self.source_id,
            place_code=self.place_code,
            source_path=Path(self.source_path)
            if self.source_path is not None
            else None,
            source_ref=self.source_ref,
            request_order_ns=self.request_order_ns,
            project_inputs=self.project_inputs,
            managed_membership=self.managed_membership,
        )

    def verify(self, layout: AgentLayout) -> None:
        """Compare every binding, including fields excluded from layout equality."""
        if self != type(self).from_layout(layout):
            raise ValueError("place code collision: conflicting source ownership")


def _canonical_path(value: object) -> Path:
    if not isinstance(value, str):
        raise ValueError("instance identity requires a canonical absolute path")
    path = Path(value)
    if not path.is_absolute() or str(path.resolve()) != value:
        raise ValueError("instance identity requires a canonical absolute path")
    return path
