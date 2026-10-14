"""Generation, merge, retention, and query correction acceptance checks."""

from dataclasses import replace
import json
import os
from concurrent.futures import ThreadPoolExecutor

import psutil
import pytest

from toolang.base.types.sandbox import SandboxRef
from toolang.common.files import file_lock_path
from toolang.common.layout import AgentLayout
from toolang.up.records import (
    read_record,
    update_record,
    patch_workload,
    SandboxState,
    management_operation,
    runtime_lock,
)
from toolang.up.process import AgentProcess, stop_runtime_state, preferred_runtime_port
from toolang.up.sandbox import resolve_selection, _clear_state


def begin(layout, identity="first"):
    update_record(
        layout.runtime_status,
        lambda entry: entry.update(launch_id=identity),
        instance=layout.instance,
    )


def test_shared_file_merges_concurrent_sections_and_isolated_instances(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    other = replace(layout, instance="script")
    begin(layout)
    begin(other, "script-launch")
    with ThreadPoolExecutor(max_workers=2) as pool:
        workload = pool.submit(
            patch_workload, layout.runtime_status, "first", {"pid": 123}
        )
        control = pool.submit(
            update_record,
            layout.runtime_status,
            lambda entry: entry["control"].update(endpoint="http://localhost:8123"),
            launch_id="first",
        )
        assert workload.result() and control.result()
    entry = read_record(layout.runtime_status)
    assert entry is not None
    assert entry["workload"]["pid"] == 123
    assert entry["control"]["endpoint"].endswith(":8123")
    assert required_record(other)["launch_id"] == "script-launch"
    assert other.runtime_status == layout.runtime_status


def test_old_workload_and_cleanup_cannot_change_a_new_launch(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    begin(layout)
    old = SandboxState("host", SandboxRef("123", "http://localhost:8123"), "first")
    old.save(layout.runtime_status)
    begin(layout, "second")
    current = SandboxState("host", SandboxRef("456", "http://localhost:8123"), "second")
    current.save(layout.runtime_status)
    assert not patch_workload(layout.runtime_status, "first", {"status": "stopped"})
    _clear_state(layout, expected=old)
    assert SandboxState.load(layout.runtime_status) == current


def test_resource_binding_cannot_adopt_a_launch_created_after_its_snapshot(
    tmp_path, monkeypatch
):
    from toolang.up import records

    layout = AgentLayout.resident(tmp_path, "alice")
    old = SandboxState("host", SandboxRef("123", "http://localhost:8123"), "first")
    update = records.update_record

    def raced_update(*args, **kwargs):
        begin(layout, "second")
        return update(*args, **kwargs)

    monkeypatch.setattr(records, "update_record", raced_update)
    with pytest.raises(ValueError, match="launch ownership changed"):
        old.save(layout.runtime_status)
    entry = required_record(layout)
    assert entry["launch_id"] == "second"
    assert "ref" not in entry["control"]


def test_stop_retains_successful_settings_and_error_history(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    begin(layout)
    update_record(
        layout.runtime_status,
        lambda entry: entry["control"].update(
            last_successful={
                "endpoint": "http://localhost:8123",
                "sandbox": "docker:python:3.13-slim",
            }
        ),
    )
    patch_workload(
        layout.runtime_status,
        "first",
        {"status": "running", "pid": 123, "message": "diagnostic"},
    )
    assert stop_runtime_state(layout, launch_id="first")
    assert not stop_runtime_state(layout, launch_id="first")
    assert preferred_runtime_port(layout) == 8123
    assert resolve_selection(layout) == "docker:python:3.13-slim"
    layout.config.write_text('[sandbox]\ndriver = "host"\n')
    assert resolve_selection(layout) == "host"
    assert resolve_selection(layout, explicit="docker:other") == "docker:other"
    assert required_record(layout)["workload"]["message"] == "diagnostic"
    assert not file_lock_path(layout.runtime_status).exists()


def test_queries_reconcile_dead_identity_once_and_keep_recovery_refs(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    begin(layout)
    ref = SandboxRef(
        str(os.getpid()),
        "http://localhost:8123",
        runtime_kind="process",
        meta={"created": 0},
    )
    state = SandboxState("host", ref, "first")
    state.save(layout.runtime_status)
    patch_workload(
        layout.runtime_status,
        "first",
        {"status": "running", "pid": os.getpid(), "process_created": 0},
    )
    owner = AgentProcess(layout)
    status = owner.status(ui_base_url="")
    assert status is not None and status.status == "stopped"
    before = layout.runtime_status.read_bytes()
    status = owner.status(ui_base_url="")
    assert status is not None and status.status == "stopped"
    assert layout.runtime_status.read_bytes() == before
    assert owner.reference == state
    assert required_record(layout)["workload"].get("observed_dead_at")


def test_query_skips_contended_or_changed_snapshot(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    begin(layout)
    patch_workload(layout.runtime_status, "first", {"status": "running", "pid": 123})
    owner = AgentProcess(layout)
    observed = read_record(layout.runtime_status)
    with runtime_lock(layout.runtime_status):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert not pool.submit(owner.reconcile, observed).result(timeout=1)
    patch_workload(layout.runtime_status, "first", {"updated_at": "changed"})
    assert not owner.reconcile(observed)
    assert required_record(layout)["workload"]["status"] == "running"


def test_management_operation_does_not_hold_status_lock(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    with management_operation(layout, stage="start"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(
                update_record,
                layout.runtime_status,
                lambda entry: entry["workload"].update(pid=123),
            ).result(timeout=1)
        assert (
            required_record(layout)["control"]["operation"]["created"]
            == psutil.Process().create_time()
        )
    assert "operation" not in required_record(layout)["control"]


def test_stopped_legacy_report_migrates_but_live_report_is_rejected(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.runtime_status.parent.mkdir(parents=True)
    layout.runtime_status.write_text(
        json.dumps(
            {
                "status": "stopped",
                "endpoint": "http://localhost:8123",
                "sandbox": "host",
            }
        )
    )
    with management_operation(layout, stage="start"):
        assert preferred_runtime_port(layout) == 8123
    layout.runtime_status.write_text(json.dumps({"status": "running", "pid": 123}))
    with pytest.raises(ValueError, match="previous Toolang version"):
        with management_operation(layout, stage="start"):
            pytest.fail("must not adopt live legacy runtime")


def required_record(layout):
    entry = read_record(layout.runtime_status, layout.instance)
    assert entry is not None
    return entry


def test_unknown_backend_probe_does_not_rewrite_workload(tmp_path, monkeypatch):
    from toolang.up import sandbox

    layout = AgentLayout.resident(tmp_path, "alice")
    begin(layout)
    state = SandboxState(
        "docker:image",
        SandboxRef("container", "http://localhost:8123", runtime_kind="container"),
        "first",
    )
    state.save(layout.runtime_status)
    patch_workload(layout.runtime_status, "first", {"status": "running", "pid": 1})
    before = layout.runtime_status.read_bytes()

    class Unknown:
        async def running(self, ref):
            raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(sandbox, "load_state_sandbox", lambda *args: Unknown())
    status = AgentProcess(layout).status(ui_base_url="")
    assert status is not None and status.status == "unknown"
    assert layout.runtime_status.read_bytes() == before


def test_pending_resource_with_live_control_owner_is_preparing(tmp_path, monkeypatch):
    from toolang.up import sandbox

    layout = AgentLayout.resident(tmp_path, "alice")
    begin(layout)
    state = SandboxState(
        "docker:image",
        SandboxRef("pending", "http://localhost:8123", runtime_kind="container"),
        "first",
    )
    state.save(layout.runtime_status)
    patch_workload(layout.runtime_status, "first", {"status": "preparing", "pid": None})

    class Uncreated:
        async def running(self, ref):
            return False

    monkeypatch.setattr(sandbox, "load_state_sandbox", lambda *args: Uncreated())
    with management_operation(layout, stage="start"):
        before = layout.runtime_status.read_bytes()
        status = AgentProcess(layout).status(ui_base_url="")
        assert status is not None and status.status == "preparing"
        assert layout.runtime_status.read_bytes() == before


def test_unreferenced_guest_pid_is_never_probed_in_the_host_namespace(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    begin(layout)
    update_record(
        layout.runtime_status,
        lambda entry: entry["control"].update(sandbox="docker:image"),
    )
    patch_workload(
        layout.runtime_status,
        "first",
        {"status": "running", "pid": os.getpid(), "process_created": 0},
    )
    before = layout.runtime_status.read_bytes()
    status = AgentProcess(layout).status(ui_base_url="")
    assert status is not None and status.status == "unknown"
    assert layout.runtime_status.read_bytes() == before


def test_stop_recovers_a_host_created_before_its_reference_was_bound(tmp_path):
    import asyncio
    import subprocess
    import sys
    from toolang.up.sandbox import stop

    layout = AgentLayout.resident(tmp_path, "alice")
    begin(layout)
    update_record(
        layout.runtime_status,
        lambda entry: entry["control"].update(
            sandbox="host", endpoint="http://localhost:8123"
        ),
    )
    patch_workload(layout.runtime_status, "first", {"status": "preparing", "pid": None})
    worker = subprocess.Popen(
        (
            sys.executable,
            "-c",
            "import time; print('created', flush=True); time.sleep(60)",
            "--launch-id",
            "first",
        ),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert (
            worker.stdout is not None and worker.stdout.readline().strip() == "created"
        )
        assert asyncio.run(stop(layout, force=True))
        worker.wait(timeout=5)
        entry = required_record(layout)
        assert entry["workload"]["status"] == "stopped"
        assert "ref" not in entry["control"]
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


def test_killed_control_owner_can_be_recovered_without_losing_settings(tmp_path):
    import subprocess
    import sys
    import time

    layout = AgentLayout.resident(tmp_path, "alice")
    script = """
import sys, time
from pathlib import Path
from toolang.common.layout import AgentLayout
from toolang.up.records import management_operation, update_record
layout = AgentLayout.resident(Path(sys.argv[1]), 'alice')
with management_operation(layout, stage='start'):
    update_record(layout.runtime_status, lambda entry: entry['control'].update(last_successful={'endpoint': 'http://localhost:8123', 'sandbox': 'host'}))
    print('owned', flush=True)
    time.sleep(60)
"""
    owner = subprocess.Popen(
        (sys.executable, "-c", script, str(tmp_path)), stdout=subprocess.PIPE, text=True
    )
    try:
        assert owner.stdout is not None and owner.stdout.readline().strip() == "owned"
        owner.kill()
        owner.wait(timeout=5)
        started = time.monotonic()
        with management_operation(layout, stage="stop"):
            assert preferred_runtime_port(layout) == 8123
        assert time.monotonic() - started < 2
        assert "operation" not in required_record(layout)["control"]
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


def test_killed_status_writer_claim_is_reclaimed(tmp_path):
    import subprocess
    import sys

    layout = AgentLayout.resident(tmp_path, "alice")
    begin(layout)
    script = """
import sys, time
from pathlib import Path
from toolang.up.records import runtime_lock
with runtime_lock(Path(sys.argv[1])):
    print('locked', flush=True)
    time.sleep(60)
"""
    writer = subprocess.Popen(
        (sys.executable, "-c", script, str(layout.runtime_status)),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert (
            writer.stdout is not None and writer.stdout.readline().strip() == "locked"
        )
        writer.kill()
        writer.wait(timeout=5)
        assert patch_workload(layout.runtime_status, "first", {"message": "recovered"})
        assert required_record(layout)["workload"]["message"] == "recovered"
        assert not file_lock_path(layout.runtime_status).exists()
    finally:
        if writer.poll() is None:
            writer.kill()
            writer.wait(timeout=5)
