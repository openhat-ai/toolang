"""Publication and cleanup boundaries preserve discovery ownership."""

from dataclasses import replace
import json

import pytest

from toolang.common.layout import AgentLayout
from toolang.up.identity import allocate_roaming
from toolang.up.instance import read_instance, update_instance
from toolang.up.records import InstanceIdentity
from toolang.up.registration import (
    RuntimeRegistration,
    read_registration,
    registration_current,
)
from toolang.up.workload import ForegroundWorkload


def initialized_registration(root):
    layout = AgentLayout.resident(root, "alice")
    update_instance(
        layout.runtime_status,
        lambda entry: entry.update(
            launch_id="generation",
            control=InstanceIdentity.from_layout(layout).to_data(),
            workload={},
        ),
    )
    return RuntimeRegistration(layout, toolang_root=root, launch_id="generation")


def test_explicit_publication_and_read_only_checks_preserve_the_runtime_record(
    tmp_path,
):
    registration = initialized_registration(tmp_path)
    status = registration.layout.runtime_status
    before = status.read_bytes()

    assert registration.publish() is registration
    assert read_registration(registration.path) == {
        "version": 5,
        "status": str(status),
        "launch_id": "generation",
    }
    reference = registration.path.read_bytes()
    paths = set(tmp_path.rglob("*"))
    assert registration_current(registration.path, "generation", status)
    assert not registration_current(registration.path, "old", status)
    assert not registration_current(registration.path, "generation", tmp_path / "other")
    assert registration.path.read_bytes() == reference
    assert set(tmp_path.rglob("*")) == paths
    registration.close()
    assert not registration.path.exists()
    assert status.read_bytes() == before


@pytest.mark.parametrize(
    "fault", ["missing", "generation", "instance", "bindings", "collision"]
)
def test_publication_requires_the_initialized_identity_without_rewriting_it(
    tmp_path, fault
):
    registration = initialized_registration(tmp_path)
    status = registration.layout.runtime_status
    if fault == "missing":
        status.unlink()
    elif fault == "generation":
        registration.token = "other"
    elif fault == "instance":
        registration.layout = replace(registration.layout, instance="other")
    elif fault == "bindings":
        update_instance(status, lambda entry: entry["control"].update(home="/foreign"))
    else:
        registration.layout = replace(registration.layout, project_inputs=True)
    before = status.read_bytes() if status.exists() else None
    with pytest.raises(ValueError, match="identity|collision"):
        registration.publish()
    assert not registration.path.exists()
    assert (status.read_bytes() if status.exists() else None) == before


def test_failed_publication_removes_only_its_partial_reference(tmp_path, monkeypatch):
    from toolang.up import registration as registrations

    registration = initialized_registration(tmp_path)
    before = registration.layout.runtime_status.read_bytes()

    def write_then_fail(path, content):
        path.write_text(content)
        raise OSError("publication failed")

    monkeypatch.setattr(registrations, "atomic_write_text", write_then_fail)
    with pytest.raises(OSError, match="publication failed"):
        registration.publish()
    assert not registration.path.exists()
    assert registration.layout.runtime_status.read_bytes() == before


def test_missing_reference_cleanup_does_not_create_discovery_directories(tmp_path):
    from toolang.up.registration import _remove_reference

    path = tmp_path / ".runtime" / "active" / "gus.json"
    _remove_reference(path, "finished", tmp_path / "status.json")
    assert not path.parent.exists()


def test_register_guard_keeps_a_new_reference_when_old_cleanup_races(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from toolang.common.files import file_write_lock
    from toolang.up.instance import update_instance

    layout = AgentLayout.resident(tmp_path, "gus")
    old = ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset())
    old.__enter__()
    guard = old.path.parent / "register.lock"
    current = RuntimeRegistration(layout, toolang_root=tmp_path, launch_id="new")
    entered = Event()

    def close_old():
        entered.set()
        old.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_write_lock(guard):
            closing = pool.submit(close_old)
            assert entered.wait(timeout=1)
            assert not closing.done()
            update_instance(
                layout.runtime_status, lambda entry: entry.update(launch_id="new")
            )
            current.publish()
        closing.result(timeout=1)
    assert current.path.exists()
    current.close()


@pytest.mark.parametrize("fault", ["foreign_status", "unsupported"])
def test_rejected_publication_preserves_an_existing_reference_even_with_equal_uuid(
    tmp_path, fault
):
    layout = allocate_roaming(tmp_path / "aid.too", toolang_root=tmp_path)
    entry = read_instance(layout.runtime_status)
    assert entry is not None
    registration = RuntimeRegistration(
        layout, toolang_root=tmp_path, launch_id=entry["launch_id"]
    )
    reference = json.loads(registration.path.read_text())
    if fault == "foreign_status":
        reference["status"] = str(tmp_path / "peer" / "status.json")
    else:
        reference["version"] = 3
    registration.path.write_text(json.dumps(reference))
    before = registration.path.read_bytes()
    with pytest.raises(ValueError):
        registration.publish()
    registration.close()
    assert registration.path.read_bytes() == before


@pytest.mark.parametrize("replacement", ["status", "uuid"])
def test_failed_publication_cleanup_checks_status_and_uuid_after_attempting_a_write(
    tmp_path, monkeypatch, replacement
):
    from toolang.up import registration as registrations

    layout = allocate_roaming(tmp_path / "aid.too", toolang_root=tmp_path)
    entry = read_instance(layout.runtime_status)
    assert entry is not None
    registration = RuntimeRegistration(
        layout, toolang_root=tmp_path, launch_id=entry["launch_id"]
    )
    peer = dict(registration.payload)
    peer.update(
        {"status": str(tmp_path / "peer" / "status.json")}
        if replacement == "status"
        else {"launch_id": "replacement"}
    )

    def replaced_then_failed(path, _content):
        path.write_text(json.dumps(peer))
        raise OSError("publication failed")

    monkeypatch.setattr(registrations, "atomic_write_text", replaced_then_failed)
    with pytest.raises(OSError, match="publication failed"):
        registration.publish()
    assert json.loads(registration.path.read_text()) == peer
