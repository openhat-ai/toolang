"""Discovery references retain their format and strict path/launch boundary."""

import json

import pytest

from toolang.up.registry import RuntimeReference, read_reference, registration_current


def test_reference_round_trip_and_both_ownership_fences(tmp_path):
    reference = RuntimeReference(tmp_path / ".runtime" / "status.json", "launch")
    payload = {
        "version": 6,
        "home": str(tmp_path),
        "launch": "launch",
        "location": "host",
        "sandbox": "host",
    }
    assert reference.to_data() == payload
    assert RuntimeReference.from_data(payload) == reference
    path = tmp_path / "alice.json"
    path.write_text(json.dumps(payload))
    assert read_reference(path) == reference
    assert registration_current(path, "launch", tmp_path / ".runtime" / "status.json")
    assert not registration_current(path, "replacement", tmp_path / "status.json")
    assert not registration_current(path, "launch", tmp_path / "other.json")


def test_format_five_reference_remains_readable(tmp_path):
    status = tmp_path / "agents/alice/.runtime/status.json"
    reference = RuntimeReference.from_data(
        {"version": 5, "status": str(status), "launch_id": "old-launch"}
    )
    assert reference.status == status and reference.launch_id == "old-launch"
    assert reference.home == tmp_path / "agents/alice"


@pytest.mark.parametrize(
    "field,value",
    [
        ("home", "agents/alice"),
        ("home", "/root/../alice"),
        ("home", "/root/./alice"),
        ("launch", ""),
        ("launch", 42),
        ("location", "docker"),
        ("location", []),
        ("sandbox", None),
        ("address", 42),
        ("address", ""),
        ("source", "relative.too"),
    ],
)
def test_info_rejects_invalid_path_views_and_generation(tmp_path, field, value):
    payload = RuntimeReference(tmp_path / ".runtime/status.json", "launch").to_data()
    payload[field] = value
    with pytest.raises(ValueError):
        RuntimeReference.from_data(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 4),
        ("version", 5.0),
        ("version", True),
        ("launch_id", None),
        ("launch_id", 1),
        ("status", None),
        ("status", "relative/status.json"),
        ("status", "/root/../status.json"),
        ("status", "/root/./status.json"),
    ],
)
def test_invalid_reference_is_rejected_without_mutating_the_file(
    tmp_path, field, value
):
    payload = {
        "version": 5,
        "status": str(tmp_path / "status.json"),
        "launch_id": "launch",
        field: value,
    }
    path = tmp_path / "alice.json"
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        read_reference(path)
    assert path.read_bytes() == before
    assert sorted(tmp_path.iterdir()) == [path]


def test_killed_host_claim_is_recovered_without_a_status_or_reference(tmp_path):
    import subprocess
    from toolang.common.layout import AgentLayout
    import sys
    from toolang.up.registry import registration_lock

    script = """
import sys, time
from pathlib import Path
from toolang.up.registry import registration_lock
from toolang.common.layout import AgentLayout
with registration_lock(Path(sys.argv[1]), AgentLayout.resident(Path(sys.argv[1]), 'alice')):
    print('owned', flush=True)
    time.sleep(60)
"""
    worker = subprocess.Popen(
        (sys.executable, "-c", script, str(tmp_path)), stdout=subprocess.PIPE, text=True
    )
    try:
        assert worker.stdout is not None and worker.stdout.readline().strip() == "owned"
        worker.kill()
        worker.wait(timeout=5)
        directory = tmp_path / ".runtime" / "active" / "alice"
        assert [path.name for path in directory.iterdir()] == ["register.lock"]
        with registration_lock(tmp_path, AgentLayout.resident(tmp_path, "alice")):
            owner = json.loads((directory / "register.lock").read_text())
            assert owner["location"] == "host"
        assert not directory.exists()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


def test_guest_cannot_reap_a_host_claim_even_with_a_dead_local_pid(monkeypatch):
    from toolang.up import instance

    monkeypatch.setattr(instance, "process_alive", lambda *args: False)
    recovery = instance.LockRecovery()
    assert not recovery.dead(
        {"location": "host", "pid": 1, "created": 0},
        {"location": "guest", "launch_id": "own"},
    )


@pytest.mark.parametrize("nested", [False, True])
def test_publication_converts_legacy_reference_without_a_duplicate_member(
    tmp_path, nested
):
    from toolang.common.layout import AgentLayout
    from toolang.up.registry import RuntimeRegistration
    from toolang.up.watcher import RuntimeWatcher
    from toolang.up.workload import ForegroundWorkload

    layout = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset()
    ) as active:
        legacy = (
            active.path.parent if nested else active.path.parent.parent
        ) / f"{layout.address}.json"
        active.path.unlink()
        legacy.write_text(
            json.dumps(
                {
                    "version": 5,
                    "status": str(layout.runtime_status),
                    "launch_id": active.token,
                }
            )
        )
        assert (
            RuntimeWatcher(tmp_path).refresh().snapshot.instances[0].state == "running"
        )
        RuntimeRegistration(
            layout, toolang_root=tmp_path, launch_id=active.token
        ).publish()
        assert active.path.exists() and not legacy.exists()
        assert len(RuntimeWatcher(tmp_path).refresh().sources) == 1


def test_repair_resolves_owner_liveness_before_either_mutation_guard(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager
    from toolang.common.files import file_lock_path
    from toolang.common.layout import AgentLayout
    from toolang.up import registry, instance
    from toolang.up.instance import read_instance
    from toolang.up.workload import ForegroundWorkload

    layout = AgentLayout.resident(tmp_path, "alice")
    held = [False]
    original = registry._registration_guard

    def prepare(*args, **kwargs):
        guard = original(*args, **kwargs)

        @contextmanager
        def acquire():
            with guard:
                held[0] = True
                try:
                    yield
                finally:
                    held[0] = False

        return acquire()

    def dead(*args):
        assert not held[0], "liveness was probed while holding the registry guard"
        return False

    with ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset()
    ) as active:
        observed = read_instance(layout.runtime_status)
        assert observed is not None
        file_lock_path(layout.runtime_status).write_text(
            json.dumps(
                {
                    "location": "host",
                    "pid": 999999,
                    "created": 0,
                }
            )
        )
        monkeypatch.setattr(registry, "_registration_guard", prepare)
        monkeypatch.setattr(registry, "process_alive", dead)
        monkeypatch.setattr(instance, "process_alive", dead)
        assert (
            registry.repair_registration(
                layout,
                observed,
                resource_dead=True,
                recovery=instance.LockRecovery(),
                reference_path=active.path,
            )
            == "retired"
        )
        assert not active.path.exists()
