"""Persisted identities retain their complete format and strict validation."""

from dataclasses import fields, replace
import json

import pytest

from toolang.common.layout import AgentLayout
from toolang.common.place_codes import roaming_place_code
from toolang.up.records import InstanceIdentity


@pytest.fixture(params=["resident", "visiting", "roaming"])
def layout(tmp_path, request):
    root = tmp_path / "root"
    if request.param == "resident":
        return AgentLayout.resident(root, "alice")
    if request.param == "visiting":
        return AgentLayout.visiting("https://example.com/alice.too", "alice", root=root)
    source = tmp_path / "alice.too"
    return AgentLayout.roaming(
        source, place_code=roaming_place_code(str(source), 123), root=root
    )


@pytest.mark.parametrize("instance", [None, "script"])
@pytest.mark.parametrize("custom_membership", [False, True])
def test_identity_round_trip_preserves_all_bindings(
    layout, instance, custom_membership
):
    layout = replace(layout, instance=instance)
    if custom_membership:
        layout = replace(
            layout,
            project_inputs=not layout.project_inputs,
            managed_membership=not layout.managed_membership,
        )
    original = InstanceIdentity.from_layout(layout)
    control = json.loads(json.dumps(original.to_data()))
    control.update(sandbox="host", endpoint="http://localhost:7001", drivers=["chat"])
    before = dict(control)

    restored = InstanceIdentity.from_data(control)

    assert restored == original
    assert InstanceIdentity.from_layout(restored.to_layout()) == original
    restored.verify(layout)
    assert control == before
    assert not {"sandbox", "endpoint", "drivers"} & restored.to_data().keys()


def test_resident_identity_keeps_the_existing_wire_fields(tmp_path):
    record = InstanceIdentity.from_layout(AgentLayout.resident(tmp_path, "alice"))
    home = tmp_path / "agents" / "alice"
    assert record.to_data() == {
        "place": str(tmp_path),
        "home": str(home),
        "store": str(home / ".runtime" / "runs.db"),
        "name": "alice",
        "placement": "resident",
        "place_code": None,
        "address": "alice",
        "runtime_instance": None,
        "source_id": None,
        "source_path": None,
        "source_ref": None,
        "request_order_ns": None,
        "source_edit_lock": str(tmp_path / ".state" / "root" / "edit.lock"),
        "root": str(tmp_path),
        "project_inputs": False,
        "managed_membership": True,
    }


@pytest.mark.parametrize("field", [field.name for field in fields(InstanceIdentity)])
@pytest.mark.parametrize("fault", ["missing", "type"])
def test_identity_rejects_every_missing_or_mistyped_field_without_mutation(
    layout, field, fault
):
    control = InstanceIdentity.from_layout(layout).to_data()
    if fault == "missing":
        control.pop(field)
    else:
        control[field] = []
    before = dict(control)
    with pytest.raises(ValueError):
        InstanceIdentity.from_data(control)
    assert control == before


@pytest.mark.parametrize(
    "field", ["project_inputs", "managed_membership", "request_order_ns"]
)
def test_identity_does_not_coerce_equal_boolean_and_integer_values(layout, field):
    control = InstanceIdentity.from_layout(layout).to_data()
    value = control[field]
    if field == "request_order_ns":
        if value is None:
            control[field] = False
        else:
            assert isinstance(value, int)
            control[field] = float(value)
    else:
        assert isinstance(value, bool)
        control[field] = int(value)
    with pytest.raises(ValueError, match="identity"):
        InstanceIdentity.from_data(control)


def test_identity_verification_includes_bindings_excluded_from_layout_equality(layout):
    conflicting = replace(layout, root=layout.root / "other")
    assert conflicting == layout
    with pytest.raises(ValueError, match="collision"):
        InstanceIdentity.from_layout(layout).verify(conflicting)
