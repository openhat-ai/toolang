"""Strict identity serialization independent of mutable runtime metadata."""

from copy import deepcopy

import pytest

from toolang.common.layout import AgentLayout
from toolang.common.place_codes import roaming_place_code
from toolang.up.identity import InstanceIdentity, identity_bindings, layout_from_control


@pytest.fixture(params=["resident", "visiting", "roaming"])
def layout(tmp_path, request):
    root = tmp_path / "root"
    source = tmp_path / "alice.too"
    return (
        AgentLayout.resident(root, "alice")
        if request.param == "resident"
        else AgentLayout.visiting("gh:example/alice", "alice", root=root)
        if request.param == "visiting"
        else AgentLayout.roaming(
            source,
            place_code=roaming_place_code(str(source), 100),
            root=root,
        )
    )


def test_identity_round_trip_excludes_mutable_control_fields(layout):
    identity = InstanceIdentity.from_layout(layout)
    bindings = identity_bindings(layout)
    control = {
        **bindings,
        "operation": {"stage": "start"},
        "ref": {"runtime_id": "resource"},
        "last_successful": {"endpoint": "http://localhost:8123"},
    }
    before = deepcopy(control)
    parsed = InstanceIdentity.from_data(control)
    assert parsed == identity
    assert parsed.to_data() == bindings
    assert identity_bindings(parsed.to_layout()) == bindings
    assert identity_bindings(layout_from_control(control)) == bindings
    assert control == before


@pytest.mark.parametrize(
    "field",
    [
        "place",
        "home",
        "store",
        "name",
        "placement",
        "place_code",
        "address",
        "runtime_instance",
        "source_id",
        "source_path",
        "source_ref",
        "request_order_ns",
        "source_edit_lock",
        "root",
        "project_inputs",
        "managed_membership",
    ],
)
@pytest.mark.parametrize("fault", ["missing", "invalid_type"])
def test_invalid_identity_bindings_are_rejected_without_mutation(layout, field, fault):
    control = identity_bindings(layout)
    if fault == "missing":
        del control[field]
    else:
        control[field] = ["invalid"]
    before = deepcopy(control)
    with pytest.raises(ValueError):
        InstanceIdentity.from_data(control)
    assert control == before


@pytest.mark.parametrize("field", ["project_inputs", "managed_membership"])
def test_identity_rejects_integers_in_boolean_bindings(layout, field):
    control = identity_bindings(layout)
    control[field] = int(bool(control[field]))
    with pytest.raises(ValueError, match="inconsistent instance identity bindings"):
        InstanceIdentity.from_data(control)
