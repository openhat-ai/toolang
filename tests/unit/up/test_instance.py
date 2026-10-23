"""Generation, merge, retention, and query correction acceptance checks."""

from toolang.up.workload import ForegroundWorkload

from dataclasses import replace
import gc
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import time

import psutil
import pytest

from toolang.base.types.sandbox import SandboxRef
from toolang.common.files import file_lock_path
from toolang.common.layout import AgentLayout
from toolang.up.instance import (
    read_document,
    read_instance,
    update_instance,
    patch_workload,
    SandboxState,
    runtime_lock,
)
from toolang.up.process import AgentProcess, preferred_runtime_port
from toolang.up.workload import management_operation, stop_runtime_state
from toolang.up.sandbox import resolve_selection, _clear_state


def begin(layout, identity="first"):
    from toolang.up.identity import identity_bindings

    update_instance(
        layout.runtime_status,
        lambda entry: entry.update(
            launch_id=identity,
            control=identity_bindings(replace(layout, root=layout.place)),
        ),
        instance=layout.instance,
    )


@pytest.mark.parametrize("bound_root", ["original", "other"])
def test_resident_root_mismatch_preserves_the_existing_runtime(tmp_path, bound_root):
    from toolang.up.workload import begin_workload

    original = AgentLayout.resident(tmp_path / "original", "alice")
    other_root = tmp_path / "other"
    layout = (
        original if bound_root == "original" else replace(original, root=other_root)
    )
    begin(original)
    before = original.runtime_status.read_bytes()
    with pytest.raises(ValueError, match="root"):
        begin_workload(
            layout,
            launch_id="replacement",
            toolang_root=other_root,
            sandbox="host",
            endpoint="",
            drivers=frozenset(),
            workload={"status": "starting"},
        )
    assert original.runtime_status.read_bytes() == before
    assert not other_root.exists()


@pytest.mark.parametrize("placement", ["resident", "visiting", "roaming"])
def test_root_alias_publishes_canonical_runtime_bindings(tmp_path, placement):
    from toolang.up.allocation import allocate_roaming
    from toolang.up.identity import layout_from_control

    root = tmp_path / "selected"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    layout = (
        AgentLayout.resident(root, "alice")
        if placement == "resident"
        else AgentLayout.visiting("gh:example/alice", "alice", root=root)
        if placement == "visiting"
        else allocate_roaming(tmp_path / "alice.too", toolang_root=root)
    )
    with ForegroundWorkload(layout, toolang_root=alias, drivers=frozenset()) as active:
        entry = read_instance(layout.runtime_status)
        assert entry is not None and active.path.exists()
        bound = layout_from_control(entry["control"])
        assert bound.root == root
        assert bound.place == layout.place
        assert entry["control"]["root"] == str(root)
    assert not active.path.exists()


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
            update_instance,
            layout.runtime_status,
            lambda entry: entry["control"].update(endpoint="http://localhost:8123"),
            launch_id="first",
        )
        assert workload.result() and control.result()
    entry = read_instance(layout.runtime_status)
    assert entry is not None
    assert entry["workload"]["pid"] == 123
    assert entry["control"]["endpoint"].endswith(":8123")
    assert required_instance(other)["launch_id"] == "script-launch"
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
    from toolang.up import instance as instances

    layout = AgentLayout.resident(tmp_path, "alice")
    old = SandboxState("host", SandboxRef("123", "http://localhost:8123"), "first")
    update = instances.update_instance

    def raced_update(*args, **kwargs):
        begin(layout, "second")
        return update(*args, **kwargs)

    monkeypatch.setattr(instances, "update_instance", raced_update)
    with pytest.raises(ValueError, match="launch ownership changed"):
        old.save(layout.runtime_status)
    entry = required_instance(layout)
    assert entry["launch_id"] == "second"
    assert "ref" not in entry["control"]


def test_stop_retains_successful_settings_and_error_history(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    begin(layout)
    update_instance(
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
    assert required_instance(layout)["workload"]["message"] == "diagnostic"
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
    assert required_instance(layout)["workload"].get("observed_dead_at")


def test_query_skips_contended_or_changed_snapshot(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    begin(layout)
    patch_workload(layout.runtime_status, "first", {"status": "running", "pid": 123})
    from toolang.up.registry import repair_registration, LockRecovery

    observed = required_instance(layout)
    with runtime_lock(layout.runtime_status):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert (
                pool.submit(
                    repair_registration,
                    layout,
                    observed,
                    resource_dead=True,
                    recovery=LockRecovery(),
                ).result(timeout=1)
                == "deferred"
            )
    patch_workload(layout.runtime_status, "first", {"updated_at": "changed"})
    assert (
        repair_registration(
            layout, observed, resource_dead=True, recovery=LockRecovery()
        )
        == "changed"
    )
    assert required_instance(layout)["workload"]["status"] == "running"


def test_management_operation_does_not_hold_status_lock(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    with management_operation(layout, stage="start"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(
                update_instance,
                layout.runtime_status,
                lambda entry: entry["workload"].update(pid=123),
            ).result(timeout=1)
        assert (
            required_instance(layout)["control"]["operation"]["created"]
            == psutil.Process().create_time()
        )
    assert "operation" not in required_instance(layout)["control"]


@pytest.mark.parametrize("status", ["stopped", "running", "starting", "preparing"])
def test_unversioned_instance_is_rejected_without_mutation(tmp_path, status):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.runtime_status.parent.mkdir(parents=True)
    layout.runtime_status.write_text(json.dumps({"status": status, "pid": 123}))
    before = layout.runtime_status.read_bytes()
    with pytest.raises(ValueError, match="unsupported runtime status version"):
        with management_operation(layout, stage="start"):
            pytest.fail("must not adopt an unversioned runtime")
    assert layout.runtime_status.read_bytes() == before
    assert not file_lock_path(layout.runtime_status).exists()


def required_instance(layout):
    entry = read_instance(layout.runtime_status, layout.instance)
    assert entry is not None
    return entry


@pytest.mark.parametrize("version", [2, 3, 4.0, True])
@pytest.mark.parametrize("reader", [read_document, read_instance])
def test_noncurrent_instance_is_rejected_without_mutation(tmp_path, version, reader):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.runtime_status.parent.mkdir(parents=True)
    layout.runtime_status.write_text(json.dumps({"version": version, "instances": {}}))
    before = layout.runtime_status.read_bytes()
    with pytest.raises(ValueError, match="unsupported runtime status version"):
        reader(layout.runtime_status)
    assert layout.runtime_status.read_bytes() == before


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
    update_instance(
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
    update_instance(
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
        entry = required_instance(layout)
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
from toolang.up.workload import management_operation
from toolang.up.instance import update_instance
layout = AgentLayout.resident(Path(sys.argv[1]), 'alice')
with management_operation(layout, stage='start'):
    update_instance(layout.runtime_status, lambda entry: entry['control'].update(last_successful={'endpoint': 'http://localhost:8123', 'sandbox': 'host'}))
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
        assert "operation" not in required_instance(layout)["control"]
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
from toolang.up.instance import runtime_lock
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
        assert required_instance(layout)["workload"]["message"] == "recovered"
        assert not file_lock_path(layout.runtime_status).exists()
    finally:
        if writer.poll() is None:
            writer.kill()
            writer.wait(timeout=5)


@pytest.mark.parametrize("alive", [None, False, True])
@pytest.mark.parametrize("worker_available", [True, False])
def test_foreign_guest_claim_requires_a_definite_dead_probe(
    tmp_path, monkeypatch, alive, worker_available, recwarn
):
    from toolang.up import instance as instances, sandbox

    layout = AgentLayout.resident(tmp_path, "alice")
    from toolang.up.identity import identity_bindings

    previous = {
        "location": "guest",
        "launch_id": "guest",
        "acquired_at": time.time() - 2,
        "control": {
            **identity_bindings(layout),
            "place": str(layout.place),
            "name": layout.name,
            "placement": layout.placement,
            "sandbox": "docker:image",
            "ref": SandboxRef(
                "container", "http://localhost:1", runtime_kind="container"
            ).to_data(),
        },
    }

    class Backend:
        async def running(self, ref):
            return alive

    @contextmanager
    def claim(path, *, owner_dead, **kwargs):
        assert owner_dead(previous) is (worker_available and alive is False)
        yield

    def reject_worker(*args, **kwargs):
        raise RuntimeError("cannot start worker thread")

    if not worker_available:
        monkeypatch.setattr(ThreadPoolExecutor, "submit", reject_worker)
    layout.runtime_status.parent.mkdir(parents=True, exist_ok=True)
    file_lock_path(layout.runtime_status).write_text(json.dumps(previous))
    monkeypatch.setattr(instances, "file_write_lock", claim)
    monkeypatch.setattr(sandbox, "load_state_sandbox", lambda *args: Backend())
    with runtime_lock(layout.runtime_status):
        pass
    gc.collect()
    assert not [warning for warning in recwarn if warning.category is RuntimeWarning]


def test_temporary_cleanup_retains_recovery_refs_and_new_generations(tmp_path):
    from toolang.up.instance import remove_finished_instance

    layout = replace(AgentLayout.resident(tmp_path, "alice"), instance="script")
    begin(layout)
    state = SandboxState("host", SandboxRef("resource", "http://localhost:1"), "first")
    state.save(layout.runtime_status, instance=layout.instance)
    patch_workload(
        layout.runtime_status,
        "first",
        {"status": "stopped", "pid": None},
        instance=layout.instance,
    )
    assert not remove_finished_instance(layout, "first")
    assert required_instance(layout)["control"]["ref"]
    begin(layout, "second")
    assert not remove_finished_instance(layout, "first")
    assert required_instance(layout)["launch_id"] == "second"


@pytest.mark.parametrize("live_owner", [False, True])
@pytest.mark.parametrize("command", ["release", "stop"])
def test_next_control_operation_recovers_only_abandoned_temporary_runtimes(
    tmp_path, monkeypatch, live_owner, command
):
    import asyncio
    from toolang.up import sandbox

    base = AgentLayout.resident(tmp_path, "alice")
    old = replace(base, instance="old")
    current = replace(base, instance="new")
    registration = ForegroundWorkload(old, toolang_root=tmp_path, drivers=frozenset())
    registration.__enter__()
    state = SandboxState(
        "docker:image",
        SandboxRef("container", "http://localhost:1", runtime_kind="container"),
        registration.token,
    )
    state.save(old.runtime_status, instance=old.instance)
    update_instance(
        old.runtime_status,
        lambda entry: entry["control"].update(
            owner={
                "pid": os.getpid(),
                "created": psutil.Process().create_time() if live_owner else 0,
            }
        ),
        instance=old.instance,
    )
    stopped = []

    class Backend:
        async def stop(self, ref, *, force=False):
            stopped.append(ref)

        async def release(self, ref):
            pass

    monkeypatch.setattr(sandbox, "load_state_sandbox", lambda *args: Backend())
    asyncio.run(
        sandbox.release_stopped(current)
        if command == "release"
        else sandbox.stop(current)
    )
    assert stopped == ([] if live_owner else [state.ref])
    assert registration.path.exists() is live_owner
    assert (read_instance(base.runtime_status, old.instance) is not None) is live_owner


def test_owned_bootstrap_is_not_mistaken_for_unreferenced_staging(tmp_path):
    import asyncio
    from toolang.up import sandbox

    base = AgentLayout.resident(tmp_path, "alice")
    old = replace(base, instance="running")
    registration = ForegroundWorkload(old, toolang_root=tmp_path, drivers=frozenset())
    registration.__enter__()
    stage = base.sandbox_stage / "owned"
    stage.mkdir(parents=True)
    state = SandboxState(
        "docker:image",
        SandboxRef(
            "container",
            "http://localhost:1",
            runtime_kind="container",
            meta={"stage_dir": str(stage)},
        ),
        registration.token,
    )
    state.save(base.runtime_status, instance=old.instance)
    update_instance(
        base.runtime_status,
        lambda entry: entry.update(
            launch_id="uncreated",
            control={
                "sandbox": "docker:image",
                "placement": "resident",
                "owner": {"pid": os.getpid(), "created": 0},
            },
            workload={"status": "preparing", "pid": None},
        ),
        instance="uncreated",
    )
    asyncio.run(sandbox.release_stopped(base))
    assert stage.exists() and registration.path.exists()
    assert read_instance(base.runtime_status, "uncreated") is None


def test_previous_runtime_status_format_is_rejected_without_rewriting(tmp_path):
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"version": 4, "instances": {}}))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unsupported runtime status version"):
        update_instance(path, lambda entry: entry.update(launch_id="new"))
    assert path.read_bytes() == before
