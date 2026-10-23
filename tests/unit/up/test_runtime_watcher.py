"""Shared membership, bounded repair, and cross-process retirement contracts."""

from contextlib import contextmanager, ExitStack
import os
import json
import shutil
import subprocess
import sys

import pytest

from toolang.base.types.sandbox import SandboxRef
from toolang.common.layout import AgentLayout
from toolang.up import registry, observation
from toolang.up.identity import identity_bindings
from toolang.up.instance import (
    LockRecovery,
    patch_workload,
    read_instance,
    SandboxState,
    update_instance,
)
from toolang.up.workload import ForegroundWorkload
from toolang.up.watcher import RuntimeWatcher, runtime_status


def required_entry(layout):
    entry = read_instance(layout.runtime_status)
    assert entry is not None
    return entry


@pytest.mark.parametrize("residents_only", [False, True])
def test_scope_includes_inactive_residents_and_only_active_visitors(
    tmp_path, residents_only
):
    resident = AgentLayout.resident(tmp_path, "alice")
    resident.home.mkdir(parents=True)
    visitor = AgentLayout.visiting(
        "https://example.com/worker.too", "worker", root=tmp_path
    )
    monitor = RuntimeWatcher(tmp_path, residents_only=residents_only)
    with ForegroundWorkload(
        visitor, toolang_root=tmp_path, drivers=frozenset({"talk"})
    ):
        first = monitor.refresh()
        assert len(first.sources) == (1 if residents_only else 2)
        assert first.snapshot.membership_complete
        assert (
            next(
                item for item in first.snapshot.instances if item.name == "alice"
            ).state
            == "stopped"
        )
    final = monitor.refresh()
    assert [item.name for item in final.snapshot.instances] == ["alice"]


def test_resident_inventory_does_not_probe_visitors(tmp_path, monkeypatch):
    home = AgentLayout.resident(tmp_path, "alice")
    home.home.mkdir(parents=True)
    visitor = AgentLayout.visiting(
        "https://example.com/worker.too", "worker", root=tmp_path
    )
    original = observation.RuntimeProbe.status
    calls = []

    def inspect(self, **kwargs):
        calls.append(self.layout.address)
        return original(self, **kwargs)

    with ForegroundWorkload(visitor, toolang_root=tmp_path, drivers=frozenset()):
        monkeypatch.setattr(observation.RuntimeProbe, "status", inspect)
        result = RuntimeWatcher(tmp_path, residents_only=True).refresh()
        assert [item.name for item in result.snapshot.instances] == ["alice"]
        assert calls == [home.address]


@pytest.mark.parametrize("phase", ["preparing", "starting", "running"])
def test_port_allocation_reserves_active_launches_before_the_api_is_ready(
    tmp_path, monkeypatch, phase
):
    from toolang.up import discovery, server

    home = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(
        home,
        toolang_root=tmp_path,
        drivers=frozenset(),
        endpoint="http://localhost:7002",
    ) as active:
        patch_workload(home.runtime_status, active.token, {"status": phase})

        def no_roster(*args, **kwargs):
            pytest.fail(
                "port allocation must inspect registrations, not resident homes"
            )

        monkeypatch.setattr(server, "_port_is_available", lambda *args: True)
        assert (
            server.resolve_runtime_port(
                host="127.0.0.1",
                explicit_port=None,
                layout=AgentLayout.resident(tmp_path, "bob"),
                temporary=False,
            )
            == 7003
        )
        monkeypatch.setattr(discovery, "agent_layouts", no_roster)
        assert (
            server.resolve_runtime_port(
                host="127.0.0.1",
                explicit_port=None,
                layout=AgentLayout.resident(tmp_path, "bob"),
                temporary=False,
            )
            == 7003
        )


def test_owner_cleanup_preserves_the_original_error_if_status_is_unreadable(tmp_path):
    home = AgentLayout.resident(tmp_path, "alice")
    with pytest.raises(RuntimeError, match="work failed"):
        with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()):
            home.runtime_status.write_text("invalid json")
            raise RuntimeError("work failed")


@pytest.mark.parametrize("query", ["single", "inventory"])
def test_queries_recover_a_dead_host_writer_without_retiring_a_live_workload(
    tmp_path, query
):
    from toolang.common.files import file_lock_path

    home = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        before = home.runtime_status.read_bytes()
        claim = file_lock_path(home.runtime_status)
        claim.write_text(
            json.dumps({"location": "host", "pid": os.getpid(), "created": 0})
        )
        status = runtime_status(home, repair=False)
        assert status is not None and status.status == "running"
        assert claim.exists(), "pure observation must leave the claim alone"

        if query == "single":
            status = runtime_status(home)
            assert status is not None and status.status == "running"
        else:
            result = RuntimeWatcher(tmp_path).refresh()
            assert [row.state for row in result.snapshot.instances] == ["running"]

        assert not claim.exists()
        assert active.path.exists()
        assert home.runtime_status.read_bytes() == before


@pytest.mark.parametrize("claims", ["status", "both"])
@pytest.mark.parametrize("alive", [False, True, None])
def test_terminal_guest_claims_share_one_recovery_probe(
    tmp_path, monkeypatch, claims, alive
):
    from toolang.common.files import file_lock_path
    from toolang.up import instance

    probes = []

    def inspect(*args):
        probes.append(args)
        return alive

    monkeypatch.setattr(instance, "sandbox_alive", inspect)
    home = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        SandboxState(
            "docker:image",
            SandboxRef("container", "http://localhost:7002", runtime_kind="container"),
            active.token,
        ).save(home.runtime_status)
        patch_workload(
            home.runtime_status, active.token, {"status": "stopped", "pid": None}
        )
        entry = required_entry(home)
        claim = {
            "location": "guest",
            "pid": 1,
            "created": 0,
            "launch_id": active.token,
            "control": entry["control"],
        }
        status_guard = file_lock_path(home.runtime_status)
        status_guard.write_text(json.dumps(claim))
        registry_guard = active.path.parent / "register.lock"
        if claims == "both":
            registry_guard.write_text(json.dumps(claim))
        result = RuntimeWatcher(tmp_path).refresh()
        assert result.snapshot.instances[0].state == "stopped"
        assert len(probes) == 1
        assert active.path.exists() == (alive is not False)
        assert status_guard.exists() == (alive is not False)
        if claims == "both":
            assert registry_guard.exists() == (alive is not False)


def test_terminal_docker_status_needs_no_liveness_probe_without_orphaned_claims(
    tmp_path, monkeypatch
):
    home = AgentLayout.resident(tmp_path, "alice")

    def batch(cls, owners):
        assert not owners, "terminal status does not need sandbox liveness"

    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        SandboxState(
            "docker:image",
            SandboxRef("container", "http://localhost:7002", runtime_kind="container"),
            active.token,
        ).save(home.runtime_status)
        patch_workload(
            home.runtime_status, active.token, {"status": "stopped", "pid": None}
        )
        monkeypatch.setattr(
            observation.RuntimeProbe, "cache_docker_states", classmethod(batch)
        )
        result = RuntimeWatcher(tmp_path).refresh()
        assert result.snapshot.instances[0].state == "stopped"
        assert not active.path.exists()


def test_different_orphaned_guest_claims_make_progress_with_one_probe_per_refresh(
    tmp_path, monkeypatch
):
    from toolang.common.files import file_lock_path
    from toolang.up import instance

    probes = []

    def absent(layout, state):
        probes.append(state.ref.runtime_id)
        return False

    monkeypatch.setattr(instance, "sandbox_alive", absent)
    home = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        SandboxState(
            "docker:image",
            SandboxRef("container", "http://localhost:7002", runtime_kind="container"),
            active.token,
        ).save(home.runtime_status)
        patch_workload(
            home.runtime_status, active.token, {"status": "stopped", "pid": None}
        )
        entry = required_entry(home)
        claim = {
            "location": "guest",
            "pid": 1,
            "created": 0,
            "launch_id": active.token,
            "control": entry["control"],
        }
        status_guard = file_lock_path(home.runtime_status)
        status_guard.write_text(json.dumps(claim))
        other = {
            **claim,
            "launch_id": "old",
            "control": {
                **entry["control"],
                "ref": SandboxRef(
                    "other-container", "http://localhost:7002", runtime_kind="container"
                ).to_data(),
            },
        }
        registry_guard = active.path.parent / "register.lock"
        registry_guard.write_text(json.dumps(other))
        monitor = RuntimeWatcher(tmp_path)
        monitor.refresh()
        assert probes == ["other-container"]
        assert not registry_guard.exists() and status_guard.exists()
        assert active.path.exists()
        monitor.refresh()
        assert probes == ["other-container", "container"]
        assert not active.path.exists() and not status_guard.exists()


def test_recovery_probe_rotates_past_a_live_guest_claim(tmp_path, monkeypatch):
    from toolang.common.files import file_lock_path
    from toolang.up import instance

    probes = []

    def inspect(layout, state):
        probes.append(state.ref.runtime_id)
        return state.ref.runtime_id == "live"

    monkeypatch.setattr(instance, "sandbox_alive", inspect)
    with ExitStack() as owners:
        active = []
        for name, resource in (("alice", "live"), ("bob", "dead")):
            home = AgentLayout.resident(tmp_path, name)
            owner = owners.enter_context(
                ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset())
            )
            active.append(owner)
            SandboxState(
                "docker:image",
                SandboxRef(resource, "http://localhost:7002", runtime_kind="container"),
                owner.token,
            ).save(home.runtime_status)
            patch_workload(
                home.runtime_status, owner.token, {"status": "stopped", "pid": None}
            )
            entry = required_entry(home)
            file_lock_path(home.runtime_status).write_text(
                json.dumps(
                    {
                        "location": "guest",
                        "pid": 1,
                        "created": 0,
                        "launch_id": owner.token,
                        "control": entry["control"],
                    }
                )
            )
        monitor = RuntimeWatcher(tmp_path)
        monitor.refresh()
        assert probes == ["live"] and all(owner.path.exists() for owner in active)
        monitor.refresh()
        assert probes == ["live", "dead"]
        assert active[0].path.exists() and not active[1].path.exists()


@pytest.mark.parametrize("fault", ["missing_sandbox", "invalid_operation"])
def test_invalid_runtime_metadata_does_not_break_a_healthy_peer(tmp_path, fault):
    home = AgentLayout.resident(tmp_path, "alice")
    peer = AgentLayout.resident(tmp_path, "bob")
    with (
        ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()),
        ForegroundWorkload(peer, toolang_root=tmp_path, drivers=frozenset()),
    ):

        def corrupt(entry):
            if fault == "missing_sandbox":
                entry["control"]["ref"] = SandboxRef(
                    "broken", "http://localhost:7002"
                ).to_data()
                entry["control"].pop("sandbox")
            else:
                entry["control"]["operation"] = "invalid"

        update_instance(home.runtime_status, corrupt)
        before = home.runtime_status.read_bytes()
        states = {
            source.instance.name: source.instance.state
            for source in RuntimeWatcher(tmp_path).refresh().sources
        }
        assert states == {"alice": "unknown", "bob": "running"}
        assert home.runtime_status.read_bytes() == before


def test_bad_registration_does_not_preserve_an_unrelated_departure(tmp_path):
    one = AgentLayout.visiting("https://example.com/one.too", "one", root=tmp_path)
    two = AgentLayout.visiting("https://example.com/two.too", "two", root=tmp_path)
    monitor = RuntimeWatcher(tmp_path)
    with (
        ForegroundWorkload(
            one, toolang_root=tmp_path, drivers=frozenset()
        ) as active_one,
        ForegroundWorkload(
            two, toolang_root=tmp_path, drivers=frozenset()
        ) as active_two,
    ):
        assert len(monitor.refresh().sources) == 2
        active_one.path.write_text("invalid json")
        active_two.close()
        result = monitor.refresh()
        assert [item.name for item in result.snapshot.instances] == ["one"]
        assert result.snapshot.instances[0].state == "unknown"
        assert not result.snapshot.membership_complete
        active_one.path.unlink()
        assert monitor.refresh().sources == ()


@pytest.mark.parametrize("stage", ["reference_read", "status_probe"])
def test_departure_during_discovery_does_not_restore_the_previous_binding(
    tmp_path, monkeypatch, stage
):
    from toolang.up import discovery

    home = AgentLayout.visiting("https://example.com/alice.too", "alice", root=tmp_path)
    monitor = RuntimeWatcher(tmp_path)
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        assert len(monitor.refresh().sources) == 1
        if stage == "reference_read":
            original_read = discovery.read_reference

            def read(path):
                active.close()
                return original_read(path)

            monkeypatch.setattr(discovery, "read_reference", read)
        else:
            original_probe = observation.RuntimeProbe.status

            def probe(self, **kwargs):
                status = original_probe(self, **kwargs)
                active.close()
                return status

            monkeypatch.setattr(observation.RuntimeProbe, "status", probe)
        result = monitor.refresh()
        assert not result.sources and result.snapshot.membership_complete
        assert result.registrations == frozenset()


def test_resident_status_failure_does_not_lose_known_membership(tmp_path):
    home = AgentLayout.resident(tmp_path, "alice")
    home.runtime_status.parent.mkdir(parents=True)
    home.runtime_status.write_text("invalid json")
    result = RuntimeWatcher(tmp_path).refresh()
    assert result.snapshot.instances[0].state == "unknown"
    assert result.snapshot.partial and result.snapshot.membership_complete


@pytest.mark.parametrize("residents_only", [False, True])
def test_registration_cannot_keep_a_removed_resident_home_in_scope(
    tmp_path, residents_only
):
    home = AgentLayout.resident(tmp_path, "alice")
    other = AgentLayout.resident(tmp_path, "bob")
    other.home.mkdir(parents=True)
    monitor = RuntimeWatcher(tmp_path, residents_only=residents_only)
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        assert len(monitor.refresh().sources) == 2
        shutil.rmtree(home.home)
        for watcher in (
            monitor,
            RuntimeWatcher(tmp_path, residents_only=residents_only),
        ):
            result = watcher.refresh()
            assert [item.name for item in result.snapshot.instances] == ["bob"]
            assert result.snapshot.membership_complete
            assert result.snapshot.diagnostics
        assert active.path.exists()  # Missing status alone cannot prove owner death.


def test_repair_contention_does_not_keep_a_proven_dead_visitor(tmp_path, monkeypatch):
    home = AgentLayout.visiting("https://example.com/alice.too", "alice", root=tmp_path)
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        monkeypatch.setattr(observation, "process_alive", lambda *args: False)

        @contextmanager
        def busy(*args, **kwargs):
            raise BlockingIOError("busy")
            yield

        monkeypatch.setattr(registry, "_registration_guard", busy)
        result = RuntimeWatcher(tmp_path).refresh()
        assert not result.sources and result.snapshot.membership_complete
        assert active.path.exists()


def test_refresh_repairs_at_most_eight_and_rotates_attempts(tmp_path, monkeypatch):
    active = []
    for index in range(20):
        home = AgentLayout.resident(tmp_path, f"agent{index}")
        active.append(
            ForegroundWorkload(
                home, toolang_root=tmp_path, drivers=frozenset()
            ).__enter__()
        )
    monkeypatch.setattr(observation, "process_alive", lambda *args: False)
    monitor = RuntimeWatcher(tmp_path)
    try:
        first = monitor.refresh()
        assert len(first.sources) == 20 and first.snapshot.membership_complete
        assert all(item.state == "stopped" for item in first.snapshot.instances)
        assert sum(item.path.exists() for item in active) == 12
        monitor.refresh()
        monitor.refresh()
        assert not any(item.path.exists() for item in active)
    finally:
        for item in active:
            item.close()


def test_repair_cannot_remove_a_launch_that_bound_after_its_probe(
    tmp_path, monkeypatch
):
    home = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        monkeypatch.setattr(observation, "process_alive", lambda *args: False)
        original = registry.runtime_lock

        @contextmanager
        def binding(*args, **kwargs):
            patch_workload(
                home.runtime_status, active.token, {"started_at": "bound after probe"}
            )
            monkeypatch.setattr(observation, "process_alive", lambda *args: True)
            with original(*args, **kwargs):
                yield

        monkeypatch.setattr(registry, "runtime_lock", binding)
        result = runtime_status(home)
        assert result is not None and result.status == "running"
        assert result.entry is not None
        assert result.entry["workload"]["started_at"] == "bound after probe"
        assert active.path.exists()
        assert required_entry(home)["workload"]["status"] == "running"


@pytest.mark.parametrize("cleanup", ["busy", "budget"])
@pytest.mark.parametrize("alive", [True, None])
def test_deferred_repair_rechecks_changed_liveness_before_publishing_membership(
    tmp_path, monkeypatch, cleanup, alive
):
    with ExitStack() as owners:
        active = [
            owners.enter_context(
                ForegroundWorkload(
                    AgentLayout.visiting(
                        f"https://example.com/agent{index}.too",
                        f"agent{index}",
                        root=tmp_path,
                    ),
                    toolang_root=tmp_path,
                    drivers=frozenset(),
                )
            )
            for index in range(9 if cleanup == "budget" else 1)
        ]
        target = active[-1]
        original = registry.repair_registration

        with monkeypatch.context() as patch:
            patch.setattr(observation, "process_alive", lambda *args: False)

            def bind():
                patch_workload(
                    target.layout.runtime_status,
                    target.token,
                    {"started_at": "bound after probe"},
                )
                patch.setattr(observation, "process_alive", lambda *args: alive)

            if cleanup == "busy":

                @contextmanager
                def busy(*args, **kwargs):
                    bind()
                    raise BlockingIOError("busy")
                    yield

                patch.setattr(registry, "_registration_guard", busy)
            else:
                attempts = []

                def repair(*args, **kwargs):
                    if not attempts:
                        bind()
                    attempts.append(args[0].address)
                    return original(*args, **kwargs)

                patch.setattr(registry, "repair_registration", repair)

            result = RuntimeWatcher(tmp_path).refresh()
            assert [item.identity for item in result.snapshot.instances] == [
                target.layout.address
            ]
            assert result.snapshot.instances[0].state == (
                "running" if alive else "unknown"
            )
            assert result.snapshot.membership_complete == (alive is True)
            if cleanup == "budget":
                assert len(attempts) == 8 and target.layout.address not in attempts
            assert target.path.exists()
            assert required_entry(target.layout)["workload"]["status"] == "running"


def test_child_binding_while_holding_the_status_guard_invalidates_a_death_probe(
    tmp_path, monkeypatch
):
    home = AgentLayout.resident(tmp_path, "alice")
    script = """
import os, psutil, sys
from pathlib import Path
from toolang.up.instance import patch_workload, runtime_lock
path, launch = Path(sys.argv[1]), sys.argv[2]
print('ready', flush=True)
sys.stdin.readline()
patch_workload(path, launch, {
    'pid': os.getpid(), 'process_created': psutil.Process().create_time(),
    'status': 'starting',
})
with runtime_lock(path):
    print('bound', flush=True)
    sys.stdin.readline()
"""
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        child = subprocess.Popen(
            (sys.executable, "-c", script, str(home.runtime_status), active.token),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdin is not None and child.stdout is not None
            commands, output = child.stdin, child.stdout
            assert output.readline().strip() == "ready"
            original_guard = registry._registration_guard
            process_alive = observation.process_alive

            with monkeypatch.context() as patch:
                patch.setattr(observation, "process_alive", lambda *args: False)

                @contextmanager
                def bind_before_repair(*args, **kwargs):
                    commands.write("bind\n")
                    commands.flush()
                    assert output.readline().strip() == "bound"
                    patch.setattr(observation, "process_alive", process_alive)
                    with original_guard(*args, **kwargs):
                        yield

                patch.setattr(registry, "_registration_guard", bind_before_repair)
                status = runtime_status(home, cache=False)
                assert status is not None and status.status == "starting"
                assert status.entry is not None
                assert status.entry["workload"]["pid"] == child.pid
                assert active.path.exists()
        finally:
            if child.stdin is not None:
                child.stdin.close()
            try:
                child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
        assert child.returncode == 0
        status = runtime_status(home, cache=False)
        assert status is not None and status.status == "stopped"
        assert not active.path.exists()


def test_status_loss_during_deferred_repair_keeps_membership_unknown(
    tmp_path, monkeypatch
):
    home = AgentLayout.visiting("https://example.com/alice.too", "alice", root=tmp_path)
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        with monkeypatch.context() as patch:
            patch.setattr(observation, "process_alive", lambda *args: False)

            @contextmanager
            def busy(*args, **kwargs):
                home.runtime_status.unlink()
                raise BlockingIOError("busy")
                yield

            patch.setattr(registry, "_registration_guard", busy)
            result = RuntimeWatcher(tmp_path).refresh()
            assert len(result.sources) == 1
            assert result.sources[0].instance.state == "unknown"
            assert not result.snapshot.membership_complete
            assert active.path.exists()


def test_delayed_child_cannot_reactivate_a_retired_launch(tmp_path, monkeypatch):
    home = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        monkeypatch.setattr(observation, "process_alive", lambda *args: False)
        status = runtime_status(home)
        assert status is not None and status.status == "stopped"
        assert not active.path.exists()
        with pytest.raises(ValueError, match="terminal"):
            patch_workload(
                home.runtime_status,
                active.token,
                {"status": "starting", "pid": os.getpid()},
            )
        with pytest.raises(ValueError, match="terminal"):
            SandboxState(
                "host", SandboxRef("delayed", "http://localhost:1"), active.token
            ).save(home.runtime_status)
        assert required_entry(home)["workload"]["status"] == "stopped"


def test_guest_owner_finalizes_through_an_explicit_mounted_registry(tmp_path):
    home = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        host_status = home.runtime_status
        # A directory mount preserves host unlink semantics even though the guest
        # sees a different path. The reference itself keeps the canonical host path.
        mounted = tmp_path / "mounted-active"
        mounted.symlink_to(active.path.parent, target_is_directory=True)
        assert registry.finalize_runtime(
            home,
            active.token,
            expected_pid=os.getpid(),
            location="guest",
            directory=mounted,
            status_path=host_status,
        )
        assert not active.path.exists()
        assert required_entry(home)["workload"]["status"] == "stopped"


def test_guest_lock_recovery_does_not_need_a_registration_or_status(
    tmp_path, monkeypatch
):
    home = AgentLayout.resident(tmp_path, "alice")
    control = {
        **identity_bindings(home),
        "sandbox": "docker:image",
        "ref": SandboxRef(
            "container", "http://localhost:1", runtime_kind="container"
        ).to_data(),
    }
    claim = {"location": "guest", "launch_id": "old", "control": control}
    recovery = LockRecovery()
    probes = []

    def absent(*args):
        probes.append(args)
        return False

    monkeypatch.setattr("toolang.up.instance.sandbox_alive", absent)
    assert recovery.dead(claim, {"location": "host"})
    assert recovery.dead(claim, {"location": "host"})
    assert len(probes) == 1 and not home.runtime_status.exists()
    other = {
        **claim,
        "control": {**control, "ref": {**control["ref"], "runtime_id": "other"}},
    }
    assert not recovery.dead(other, {"location": "host"})
    assert not recovery.dead(claim, {"location": "guest", "launch_id": "other"})


def test_membership_failure_recovers_without_freezing_known_departures(
    tmp_path, monkeypatch
):
    from toolang.up import discovery
    from toolang.up.observation import RuntimeInventory
    from toolang.up.types import RuntimeInstanceSnapshot

    home = AgentLayout.resident(tmp_path, "alice")
    home.home.mkdir(parents=True)
    visitor = AgentLayout.visiting(
        "https://example.com/worker.too", "worker", root=tmp_path
    )
    monitor = RuntimeWatcher(tmp_path)
    with ForegroundWorkload(
        visitor, toolang_root=tmp_path, drivers=frozenset()
    ) as active:
        assert len(monitor.refresh().sources) == 2
        active.close()
        with monkeypatch.context() as patch:
            patch.setattr(
                discovery,
                "registered_inventory",
                lambda *args, **kwargs: RuntimeInventory(
                    RuntimeInstanceSnapshot((), ("registry unreadable",), False),
                    (),
                    None,
                ),
            )
            unknown = monitor.refresh()
            assert (
                len(unknown.sources) == 2 and not unknown.snapshot.membership_complete
            )
        result = monitor.refresh()
        assert [row.name for row in result.snapshot.instances] == ["alice"]
        assert result.snapshot.membership_complete
        with monkeypatch.context() as patch:

            def denied(*args, **kwargs):
                raise PermissionError("resident homes unreadable")

            patch.setattr(discovery, "agent_layouts", denied)
            result = monitor.refresh()
            assert len(result.sources) == 1 and not result.snapshot.membership_complete


@pytest.mark.parametrize("fault", ["foreign_name", "missing_address", "missing_home"])
def test_single_home_status_rejects_invalid_identity_without_repair(tmp_path, fault):
    home = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:

        def corrupt(entry):
            if fault == "foreign_name":
                entry["control"]["name"] = "bob"
            else:
                entry["control"].pop(fault.removeprefix("missing_"))

        update_instance(home.runtime_status, corrupt)
        before = home.runtime_status.read_bytes()
        status = runtime_status(home)
        assert status is not None and status.status == "unknown"
        assert status.entry is None
        assert home.runtime_status.read_bytes() == before
        assert active.path.exists()
