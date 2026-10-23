"""Shared membership, bounded repair, and cross-process retirement contracts."""

from contextlib import contextmanager
import os

import pytest

from toolang.base.types.sandbox import SandboxRef
from toolang.common.layout import AgentLayout
from toolang.up import registry, watcher
from toolang.up.identity import identity_bindings
from toolang.up.instance import (
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


def test_resident_status_failure_does_not_lose_known_membership(tmp_path):
    home = AgentLayout.resident(tmp_path, "alice")
    home.runtime_status.parent.mkdir(parents=True)
    home.runtime_status.write_text("invalid json")
    result = RuntimeWatcher(tmp_path).refresh()
    assert result.snapshot.instances[0].state == "unknown"
    assert result.snapshot.partial and result.snapshot.membership_complete


def test_repair_contention_does_not_keep_a_proven_dead_visitor(tmp_path, monkeypatch):
    home = AgentLayout.visiting("https://example.com/alice.too", "alice", root=tmp_path)
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        monkeypatch.setattr(watcher, "process_alive", lambda *args: False)

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
    monkeypatch.setattr(watcher, "process_alive", lambda *args: False)
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
        monkeypatch.setattr(watcher, "process_alive", lambda *args: False)
        original = registry.runtime_lock

        @contextmanager
        def binding(*args, **kwargs):
            patch_workload(
                home.runtime_status, active.token, {"started_at": "bound after probe"}
            )
            monkeypatch.setattr(watcher, "process_alive", lambda *args: True)
            with original(*args, **kwargs):
                yield

        monkeypatch.setattr(registry, "runtime_lock", binding)
        result = runtime_status(home)
        assert result is not None and result.status == "running"
        assert result.entry is not None
        assert result.entry["workload"]["started_at"] == "bound after probe"
        assert active.path.exists()
        assert required_entry(home)["workload"]["status"] == "running"


def test_delayed_child_cannot_reactivate_a_retired_launch(tmp_path, monkeypatch):
    home = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        monkeypatch.setattr(watcher, "process_alive", lambda *args: False)
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
    recovery = registry.LockRecovery()
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
    from toolang.up import observation
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
                observation,
                "_registered_inventory",
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


def test_single_home_status_rejects_foreign_identity_without_repair(tmp_path):
    home = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(home, toolang_root=tmp_path, drivers=frozenset()) as active:
        update_instance(
            home.runtime_status, lambda entry: entry["control"].update(name="bob")
        )
        before = home.runtime_status.read_bytes()
        status = runtime_status(home)
        assert status is not None and status.status == "unknown"
        assert status.entry is None
        assert home.runtime_status.read_bytes() == before
        assert active.path.exists()
