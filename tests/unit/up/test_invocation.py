"""Foreground interruption targets only the controller's current launch."""

import math

import pytest

from toolang.common.layout import AgentLayout
from toolang.up.closing import FINALIZATION_GRACE_SEC
from toolang.up.identity import identity_bindings
from toolang.up.instance import initialize_instance, read_instance
from toolang.up.invocation import ForegroundServerOwner
from toolang.up.workload import management_operation


def _prepare(layout, launch_id):
    initialize_instance(
        layout.runtime_status,
        launch_id=launch_id,
        control=identity_bindings(layout),
        workload={"status": "preparing"},
    )


@pytest.mark.parametrize("ready", [False, True])
def test_server_interruption_publishes_one_deadline_before_or_after_ready(
    tmp_path, ready
):
    layout = AgentLayout.resident(tmp_path, "alice")
    _prepare(layout, "first")
    with ForegroundServerOwner(layout) as owner:
        owner.closing.enabled = False
        with management_operation(layout, stage="serve"):
            if ready:
                owner.bind("first")
            else:
                owner.closing.begin("server interrupted")
        if ready:
            owner.closing.begin("server interrupted")

        first = read_instance(layout.runtime_status)
        assert first is not None
        started = first["workload"]["shutdown_started_at"]
        assert first["workload"]["shutdown_reason"] == "server interrupted"
        marker = layout.runtime / "owner-first.closing"
        assert int(marker.read_text()) == math.ceil(started + FINALIZATION_GRACE_SEC)

        owner.closing.begin("second interruption")
        assert read_instance(layout.runtime_status) == first
        assert int(marker.read_text()) == math.ceil(started + FINALIZATION_GRACE_SEC)


@pytest.mark.parametrize("bound", [False, True])
def test_unowned_or_replaced_launch_cannot_receive_old_closing_intent(tmp_path, bound):
    layout = AgentLayout.resident(tmp_path, "alice")
    _prepare(layout, "first")
    with ForegroundServerOwner(layout) as owner:
        owner.closing.enabled = False
        if bound:
            owner.bind("first")
            _prepare(layout, "replacement")
        before = layout.runtime_status.read_bytes()

        owner.closing.begin("late interruption")

        assert layout.runtime_status.read_bytes() == before
        assert not list(layout.runtime.glob("owner-*.closing"))
