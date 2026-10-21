from toolang.up.workload import ForegroundWorkload
from dataclasses import replace
import asyncio
import hashlib
import json
import os

import pytest

from toolang.common.layout import AgentLayout
from toolang.up.identity import allocate_roaming
from toolang.execution.store import RunStore
from toolang.up.observation import (
    RuntimeRegistration,
    instance_filename,
    running_instances,
    runtime_inventory,
    runtime_snapshot,
)


def _roaming(source):
    return allocate_roaming(source, toolang_root=source.parent)


def test_reference_names_use_stable_host_source_identity(tmp_path):
    root = tmp_path / "toolang"
    resident = AgentLayout.resident(root, "gus")
    source = "https://example.com/lex.too"
    visiting = AgentLayout.visiting(source, "lex", toolang_root=root)
    visiting_hash = hashlib.sha256(f"{root.resolve()}\0{source}".encode()).hexdigest()
    roaming = _roaming(tmp_path / "aid.too")

    assert instance_filename(resident) == "gus.json"
    assert instance_filename(visiting) == f"lex@v{visiting_hash[:16]}.json"
    assert visiting.root.name == f"lex@v{visiting_hash[:16]}"
    assert visiting.source_id == visiting_hash[:16]
    assert instance_filename(roaming) == f"aid@{roaming.instance_id}.json"
    assert instance_filename(replace(roaming)) == instance_filename(roaming)
    assert instance_filename(
        _roaming(tmp_path / "other" / "aid.too")
    ) != instance_filename(roaming)
    assert instance_filename(
        AgentLayout.visiting(source, "lex", toolang_root=root / "other")
    ) != instance_filename(visiting)
    assert instance_filename(
        AgentLayout.visiting("https://other.example/lex.too", "lex", toolang_root=root)
    ) != instance_filename(visiting)


def test_source_path_aliases_preserve_reference_identity(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "aid.too"
    source.touch()
    alias = tmp_path / "alias.too"
    alias.symlink_to(source)
    original = _roaming(source)
    assert instance_filename(
        AgentLayout.roaming(alias, instance_id=original.instance_id)
    ) == instance_filename(original)
    root_alias = tmp_path / "root-alias"
    root_alias.symlink_to(project, target_is_directory=True)
    url = "https://example.com/lex.too"
    assert instance_filename(
        AgentLayout.visiting(url, "lex", toolang_root=root_alias)
    ) == instance_filename(AgentLayout.visiting(url, "lex", toolang_root=project))


@pytest.mark.parametrize("placement", ["visiting", "roaming"])
def test_relaunch_reuses_reference_name_and_preserves_new_registration(
    tmp_path, placement
):
    from toolang.up.instance import read_instance

    layout = (
        AgentLayout.visiting(
            "https://example.com/lex.too", "lex", toolang_root=tmp_path
        )
        if placement == "visiting"
        else _roaming(tmp_path / "aid.too")
    )
    old = ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset())
    current = ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset())
    with old, current:
        assert old.path == current.path
        assert old.token != current.token
        record = read_instance(layout.runtime_status)
        assert record is not None and record["control"]["source_id"] == layout.source_id
        old.close()
        assert running_instances(tmp_path)[0]["source_id"] == layout.source_id
        assert current.path.exists()


def test_reference_names_cannot_alias_agent_names_with_markers(tmp_path):
    visiting = AgentLayout.visiting(
        "https://example.com/lex.too", "lex", toolang_root=tmp_path
    )
    with pytest.raises(ValueError, match="invalid agent name"):
        AgentLayout.resident(
            tmp_path, instance_filename(visiting).removesuffix(".json")
        )


def test_discovery_ignores_non_object_references_and_cleanup_tolerates_them(tmp_path):
    layout = AgentLayout.resident(tmp_path, "gus")
    registration = ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset()
    )
    registration.__enter__()
    for value in (None, [], "invalid", 123):
        registration.path.write_text(json.dumps(value))
        assert running_instances(tmp_path) == ()
        registration.close()


def test_discovery_does_not_adopt_previous_reference_names(tmp_path):
    layout = AgentLayout.resident(tmp_path, "gus")
    with ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset()
    ) as registration:
        old_path = registration.path.with_name("gus-resident-0123456789abcdef.json")
        registration.path.rename(old_path)
        assert running_instances(tmp_path) == ()


@pytest.mark.parametrize("instances", [None, [], "invalid", 123])
def test_discovery_ignores_invalid_status_instances(tmp_path, instances):
    layout = AgentLayout.resident(tmp_path, "gus")
    with ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset()):
        before = layout.runtime_status.read_bytes()
        try:
            layout.runtime_status.write_text(
                json.dumps({"version": 3, "instances": instances})
            )
            assert running_instances(tmp_path) == ()
        finally:
            layout.runtime_status.write_bytes(before)


@pytest.mark.parametrize("owner", ["reference", "status"])
@pytest.mark.parametrize("version", [2, 4, 3.0, True])
def test_discovery_rejects_noncurrent_formats_without_mutation(
    tmp_path, owner, version
):
    root = tmp_path / "toolang"
    layout = _roaming(tmp_path / "aid.too")
    with ForegroundWorkload(
        layout, toolang_root=root, drivers=frozenset()
    ) as registration:
        assert len(running_instances(root)) == 1
        path = registration.path if owner == "reference" else layout.runtime_status
        original = path.read_bytes()
        document = json.loads(original)
        document["version"] = version
        path.write_text(json.dumps(document))
        before = path.read_bytes()
        try:
            assert running_instances(root) == ()
            assert all(
                item.state != "running" for item in runtime_snapshot(root).instances
            )
            assert path.read_bytes() == before
        finally:
            path.write_bytes(original)


@pytest.mark.parametrize("invalid", [None, [], "invalid", {"launch_id": "bad"}])
def test_discovery_preserves_valid_entries_beside_a_malformed_peer(tmp_path, invalid):
    layout = AgentLayout.resident(tmp_path, "gus")
    with ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset()):
        before = layout.runtime_status.read_bytes()
        try:
            document = json.loads(before)
            document["instances"]["bad"] = invalid
            layout.runtime_status.write_text(json.dumps(document))
            assert len(running_instances(tmp_path)) == 1
        finally:
            layout.runtime_status.write_bytes(before)


def test_discovery_reads_do_not_wait_for_reference_updates(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from toolang.common.files import file_write_lock

    layout = AgentLayout.resident(tmp_path, "gus")
    with ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset()
    ) as registration:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with file_write_lock(registration.path.parent / "register.lock"):
                assert (
                    len(pool.submit(running_instances, tmp_path).result(timeout=1)) == 1
                )


def test_independent_script_instances_share_status_and_keep_separate_execution_stores(
    tmp_path,
) -> None:
    source = tmp_path / "app.too"
    one = _roaming(source)
    two = _roaming(source)
    assert one.home != two.home
    assert one.run_store != two.run_store
    assert one.chore_store == one.run_store
    assert one.chore_store != two.chore_store
    assert one.runtime_status != two.runtime_status
    store = RunStore(one.run_store)
    store.create_thread(thread_id="talk_existing")
    store.close()
    restarted = RunStore(replace(one).run_store)
    independent = RunStore(two.run_store)
    try:
        assert restarted.get_thread(thread_id="talk_existing") is not None
        assert independent.list_threads() == []
    finally:
        restarted.close()
        independent.close()


def test_stale_registration_and_replaced_owner_are_safe(tmp_path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    old = ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset())
    current = ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset(("chat",))
    )
    old.__enter__()
    current.__enter__()
    old.close()
    assert running_instances(tmp_path)[0]["drivers"] == ["chat"]
    assert runtime_snapshot(tmp_path).instances[0].drivers == ("chat",)
    from toolang.up.instance import patch_workload

    patch_workload(layout.runtime_status, current.token, {"process_created": 0})
    assert running_instances(tmp_path) == ()
    current.close()


def test_guest_discovery_uses_host_sandbox_ownership_not_guest_pid(
    tmp_path, monkeypatch
):
    from toolang.base.types.sandbox import SandboxRef
    from toolang.up import sandbox
    from toolang.up.instance import SandboxState
    from toolang.up.process import write_runtime_state

    layout = AgentLayout.resident(tmp_path, "guest")
    layout.home.mkdir(parents=True)
    alive = True

    class Backend:
        async def running(self, ref):
            return alive

    monkeypatch.setattr(sandbox, "load_state_sandbox", lambda *a: Backend())
    from toolang.plugin.sandboxes.docker import _cli

    monkeypatch.setattr(
        _cli,
        "docker_container_states",
        lambda ids: {identity: alive for identity in ids},
    )
    with ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset({"chore"})
    ) as registration:
        SandboxState(
            "docker:image",
            SandboxRef("container-one", "http://localhost:1", runtime_kind="container"),
            registration.token,
        ).save(layout.runtime_status)
        write_runtime_state(
            layout,
            endpoint="http://guest:1",
            started_at="now",
            pid=1,
            sandbox="docker:image",
            launch_id=registration.token,
        )
        assert running_instances(tmp_path)[0]["drivers"] == ["chore"]
        alive = False
        # Record changes invalidate the top cache immediately.
        from toolang.up.instance import patch_workload

        patch_workload(
            layout.runtime_status, registration.token, {"updated_at": "later"}
        )
        assert running_instances(tmp_path) == ()


def test_unbound_guest_discovery_preserves_unknown_workload(tmp_path):
    from toolang.up.instance import patch_workload, update_instance

    layout = AgentLayout.resident(tmp_path, "guest")
    with ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset()
    ) as registration:
        update_instance(
            layout.runtime_status,
            lambda entry: entry["control"].update(sandbox="docker:image"),
        )
        patch_workload(
            layout.runtime_status, registration.token, {"process_created": 0}
        )
        before = layout.runtime_status.read_bytes()
        assert running_instances(tmp_path) == ()
        assert layout.runtime_status.read_bytes() == before


def test_short_lived_scripts_keep_local_status_and_share_registry_lock(tmp_path):
    from toolang.up.instance import read_instance

    scope = tmp_path / "toolang"
    directory = scope / ".runtime" / "instances"
    for _ in range(20):
        layout = allocate_roaming(tmp_path / "echo.too", toolang_root=scope)
        with ForegroundWorkload(
            layout, toolang_root=scope, drivers=frozenset()
        ) as registration:
            assert registration.path.name == f"{layout.instance_name}.json"
        entry = read_instance(layout.runtime_status)
        assert entry is not None and entry["workload"]["status"] == "stopped"
    assert tuple(path.name for path in directory.iterdir()) == ("register.lock",)


@pytest.mark.parametrize("legacy_guard", ["update.lock", None])
def test_cleanup_creates_register_guard_for_an_existing_reference(
    tmp_path, legacy_guard
):
    from toolang.up.process import stop_runtime_state

    layout = AgentLayout.resident(tmp_path, "gus")
    registration = ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset()
    )
    registration.__enter__()
    stop_runtime_state(layout, expected_pid=os.getpid(), launch_id=registration.token)
    guard = registration.path.parent / "register.lock"
    if legacy_guard is None:
        guard.unlink()
    else:
        guard.rename(guard.with_name(legacy_guard))
    assert running_instances(tmp_path) == ()
    assert not guard.exists()

    registration.close()

    assert not registration.path.exists()
    assert guard.exists()
    if legacy_guard is not None:
        assert guard.with_name(legacy_guard).exists()


def test_missing_reference_cleanup_does_not_create_discovery_directories(tmp_path):
    from toolang.up.observation import _remove_reference

    path = tmp_path / ".runtime" / "instances" / "gus.json"
    _remove_reference(path, "finished")
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
            current.__enter__()
        closing.result(timeout=1)
    assert current.path.exists()
    current.close()


@pytest.mark.parametrize("placement", ["visiting", "roaming"])
def test_placement_free_workload_cleans_the_reference_using_control_bindings(
    tmp_path, placement
):
    from toolang.plugin.sandboxes.host import process_ref
    from toolang.up.instance import SandboxState
    from toolang.up.sandbox import _clear_state

    layout = (
        AgentLayout.visiting(
            "https://example.com/alice.too", "alice", toolang_root=tmp_path
        )
        if placement == "visiting"
        else _roaming(tmp_path / "alice.too")
    )
    with ForegroundWorkload(
        layout, toolang_root=tmp_path / "toolang", drivers=frozenset()
    ) as registration:
        ref = SandboxState(
            "host",
            process_ref(os.getpid(), "http://localhost:8123"),
            registration.token,
        )
        ref.save(layout.runtime_status)
        runtime_layout = replace(
            layout, placement="resident", source_id=None, instance_id=None
        )
        _clear_state(runtime_layout, expected=ref)
        assert not registration.path.exists()


def test_discovery_reads_a_shared_agent_document_once_per_refresh(
    tmp_path, monkeypatch
):
    from toolang.up import observation, instance as records
    from contextlib import ExitStack

    with ExitStack() as scope:
        for index in range(10):
            scope.enter_context(
                ForegroundWorkload(
                    _roaming(tmp_path / "echo.too"),
                    toolang_root=tmp_path,
                    drivers=frozenset(),
                )
            )
        read = records.read_document
        reads = 0

        def count_read(path, **kwargs):
            nonlocal reads
            reads += 1
            return read(path, **kwargs)

        monkeypatch.setattr(records, "read_document", count_read)
        monkeypatch.setattr(observation, "read_document", count_read)
        assert len(running_instances(tmp_path)) == 10
        assert reads == 10


def test_agent_removal_retains_home_used_by_an_independent_script(tmp_path):
    from toolang.up.sandbox import remove_agent

    layout = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(
        replace(layout, instance="script"), toolang_root=tmp_path, drivers=frozenset()
    ):
        with pytest.raises(ValueError, match="execution instance"):
            asyncio.run(remove_agent(layout))
        assert layout.home.exists()


def test_runtime_snapshot_lists_residents_and_registered_placements_once(tmp_path):
    root = tmp_path / "toolang"
    resident = AgentLayout.resident(root, "alice")
    resident.home.mkdir(parents=True)
    never_started = AgentLayout.resident(root, "bob")
    never_started.home.mkdir(parents=True)
    visiting = AgentLayout.visiting(
        "https://example.com/alice.too", "alice", toolang_root=root
    )
    roaming = _roaming(tmp_path / "alice.too")

    with (
        ForegroundWorkload(
            resident, toolang_root=root, drivers=frozenset({"chat", "talk"})
        ),
        ForegroundWorkload(visiting, toolang_root=root, drivers=frozenset({"chat"})),
        ForegroundWorkload(roaming, toolang_root=root, drivers=frozenset()),
    ):
        snapshot = runtime_snapshot(root)

    assert not snapshot.partial
    assert len(snapshot.instances) == 3
    running = [item for item in snapshot.instances if item.state == "running"]
    assert len(running) == 3
    assert (
        sum(item.drivers is not None and "talk" in item.drivers for item in running)
        == 1
    )
    assert {item.placement for item in running} == {"resident", "visiting", "roaming"}
    assert running[0].drivers is not None and "talk" in running[0].drivers
    assert all(
        item.drivers is None or "talk" not in item.drivers for item in running[1:]
    )
    assert len({item.label for item in running}) == 3
    assert len({item.identity for item in running}) == 3
    assert all(item.name != "bob" for item in snapshot.instances)


def test_runtime_snapshot_reports_starting_and_invalid_drivers_as_partial(tmp_path):
    from toolang.up.instance import update_instance

    root = tmp_path / "toolang"
    layout = AgentLayout.resident(root, "alice")
    layout.home.mkdir(parents=True)
    with ForegroundWorkload(
        layout, toolang_root=root, drivers=frozenset({"chat", "talk"})
    ) as registration:
        update_instance(
            layout.runtime_status,
            lambda entry: entry["workload"].update(status="starting"),
            launch_id=registration.token,
        )
        starting = runtime_snapshot(root)
        row = starting.instances[0]
        assert row.state == "unknown"
        assert row.observed_state == "starting"
        assert row.drivers is None and row.diagnostic is not None
        assert starting.partial

        update_instance(
            layout.runtime_status,
            lambda entry: (
                entry["workload"].update(status="running"),
                entry["control"].pop("drivers", None),
            ),
            launch_id=registration.token,
        )
        invalid = runtime_snapshot(root)
        row = invalid.instances[0]
        assert row.state == "running"
        assert (
            row.drivers is None
            and row.diagnostic == "effective launch drivers are missing or invalid"
        )
        assert invalid.partial


def test_runtime_snapshot_reads_a_shared_status_document_once(tmp_path, monkeypatch):
    import toolang.up.observation as observation

    root = tmp_path / "toolang"
    layout = AgentLayout.resident(root, "alice")
    layout.home.mkdir(parents=True)
    with ForegroundWorkload(layout, toolang_root=root, drivers=frozenset({"talk"})):
        original = observation.read_document
        reads = []

        def counted(path, *, location="host"):
            reads.append(path)
            return original(path, location=location)

        monkeypatch.setattr(observation, "read_document", counted)
        snapshot = runtime_snapshot(root)
        assert len(snapshot.instances) == 1
        assert reads.count(layout.runtime_status.resolve()) == 1


def test_unregistered_residents_do_not_enter_inventory(tmp_path):
    valid = AgentLayout.resident(tmp_path, "alice")
    valid.home.mkdir(parents=True)
    malformed = AgentLayout.resident(tmp_path, "bob")
    malformed.runtime_status.parent.mkdir(parents=True)
    malformed.runtime_status.write_text("not json")
    snapshot = runtime_snapshot(tmp_path)
    assert snapshot.instances == () and not snapshot.partial


def test_registration_can_disappear_between_listing_and_reading(tmp_path, monkeypatch):
    from pathlib import Path

    layout = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset()
    ) as registration:
        original = Path.read_text
        departed = False

        def read(path, *args, **kwargs):
            nonlocal departed
            if path == registration.path and not departed:
                departed = True
                registration.close()
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
        inventory = runtime_inventory(tmp_path)
        assert departed
        assert not inventory.snapshot.instances and not inventory.snapshot.partial
        assert not inventory.sources


@pytest.mark.parametrize("outcome", ["missing", "error", "stale"])
def test_departure_during_status_read_is_not_an_observation_failure(
    tmp_path, monkeypatch, outcome
):
    from toolang.up import observation

    layout = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset()
    ) as registration:
        original = observation.read_document

        def read(path):
            snapshot = original(path)
            registration.close()
            if outcome == "error":
                raise PermissionError("obsolete status read")
            return snapshot if outcome == "stale" else None

        monkeypatch.setattr(observation, "read_document", read)
        inventory = runtime_inventory(tmp_path)
        assert not inventory.snapshot.instances and not inventory.snapshot.partial
        assert not inventory.sources


@pytest.mark.parametrize("outcome", ["missing", "error", "stale"])
@pytest.mark.parametrize("restart", [False, True])
def test_departed_probe_cannot_publish_failure_or_a_replacement_generation(
    tmp_path, monkeypatch, outcome, restart
):
    from toolang.up.process import AgentProcess

    layout = AgentLayout.resident(tmp_path, "alice")
    with (
        ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset()) as old,
    ):
        current = ForegroundWorkload(
            layout, toolang_root=tmp_path, drivers=frozenset({"chat"})
        )
        original = AgentProcess.status

        def inspect(self, **kwargs):
            status = original(self, **kwargs)
            old.close()
            if restart:
                current.__enter__()
            if outcome == "error":
                raise OSError("obsolete probe failed")
            return status if outcome == "stale" else None

        monkeypatch.setattr(AgentProcess, "status", inspect)
        try:
            inventory = runtime_inventory(tmp_path)
            assert not inventory.snapshot.instances and not inventory.snapshot.partial
            assert not inventory.sources
            monkeypatch.setattr(AgentProcess, "status", original)
            following = runtime_inventory(tmp_path)
            assert len(following.sources) == int(restart)
            if restart:
                assert following.snapshot.instances[0].drivers == ("chat",)
                assert current.path.exists()
        finally:
            current.close()


def test_runtime_snapshot_caps_independent_liveness_probes_at_eight(
    tmp_path, monkeypatch
):
    from threading import Lock
    import time

    from toolang.up.process import AgentProcess, AgentStatus

    root = tmp_path / "toolang"
    registrations = []
    for index in range(20):
        layout = AgentLayout.resident(root, f"agent{index}")
        layout.home.mkdir(parents=True)
        registration = ForegroundWorkload(
            layout, toolang_root=root, drivers=frozenset({"talk"})
        )
        registration.__enter__()
        registrations.append(registration)

    active = 0
    maximum = 0
    lock = Lock()

    def inspect(self, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return AgentStatus(self.layout.name, "running", None, None, None, None)

    monkeypatch.setattr(AgentProcess, "status", inspect)
    try:
        snapshot = runtime_snapshot(root)
    finally:
        for registration in registrations:
            registration.close()

    assert len(snapshot.instances) == 20
    assert all(item.state == "running" for item in snapshot.instances)
    assert maximum == 8


def test_runtime_snapshot_batches_docker_liveness_references(tmp_path, monkeypatch):
    import time

    from toolang.base.types.sandbox import SandboxRef
    from toolang.up.process import AgentProcess, AgentStatus
    from toolang.up.instance import SandboxState

    root = tmp_path / "toolang"
    registrations = []
    for index in range(3):
        layout = AgentLayout.resident(root, f"agent{index}")
        layout.home.mkdir(parents=True)
        registration = ForegroundWorkload(
            layout, toolang_root=root, drivers=frozenset({"talk"})
        )
        registration.__enter__()
        registrations.append(registration)
        SandboxState(
            "docker:image",
            SandboxRef(
                f"container-{index}",
                f"http://127.0.0.1:{7000 + index}",
                runtime_kind="container",
            ),
            registration.token,
        ).save(layout.runtime_status)

    batches = []
    active = 0
    maximum = 0

    def batch(cls, owners):
        batches.append(tuple(reference.ref.runtime_id for _, reference, _ in owners))

    def inspect(self, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        time.sleep(0.01)
        active -= 1
        return AgentStatus(
            self.layout.name, "running", None, None, None, "docker:image"
        )

    monkeypatch.setattr(AgentProcess, "cache_docker_states", classmethod(batch))
    monkeypatch.setattr(AgentProcess, "status", inspect)
    try:
        snapshot = runtime_snapshot(root)
    finally:
        for registration in registrations:
            registration.close()

    assert len(batches) == 1
    assert batches[0] == ("container-0", "container-1", "container-2")
    assert len(snapshot.instances) == 3
    assert maximum <= 3


def test_runtime_snapshot_preserves_distinct_roaming_instance_labels(tmp_path):
    one = _roaming(tmp_path / "alice.too")
    two = _roaming(tmp_path / "alice.too")
    with (
        ForegroundWorkload(one, toolang_root=tmp_path, drivers=frozenset()),
        ForegroundWorkload(two, toolang_root=tmp_path, drivers=frozenset()),
    ):
        snapshot = runtime_snapshot(tmp_path)
    assert len(snapshot.instances) == 2
    assert {item.identity for item in snapshot.instances} == {
        one.instance_name,
        two.instance_name,
    }


@pytest.mark.parametrize("terminal_state", ["stopped", "failed"])
def test_runtime_snapshot_keeps_failed_residents_and_excludes_finished_visitors(
    tmp_path, terminal_state
):
    from toolang.up.instance import update_instance

    root = tmp_path / "toolang"
    resident = AgentLayout.resident(root, "alice")
    resident.home.mkdir(parents=True)
    visiting = AgentLayout.visiting(
        "https://example.com/visitor.too", "visitor", toolang_root=root
    )
    with (
        ForegroundWorkload(
            resident, toolang_root=root, drivers=frozenset({"talk"})
        ) as resident_run,
        ForegroundWorkload(
            visiting, toolang_root=root, drivers=frozenset({"talk"})
        ) as visiting_run,
    ):
        for layout, registration in (
            (resident, resident_run),
            (visiting, visiting_run),
        ):
            update_instance(
                layout.runtime_status,
                lambda entry: entry["workload"].update(status=terminal_state, pid=None),
                instance=layout.instance,
                launch_id=registration.token,
            )
        snapshot = runtime_snapshot(root)

    assert snapshot.instances == ()


@pytest.mark.parametrize("placement", ["visiting", "roaming"])
def test_first_liveness_probe_excludes_a_departed_temporary_instance(
    tmp_path, monkeypatch, placement
):
    from toolang.up.process import AgentProcess, AgentStatus

    resident = AgentLayout.resident(tmp_path, "alice")
    resident.home.mkdir(parents=True)
    temporary = (
        AgentLayout.visiting(
            "https://example.com/worker.too", "worker", toolang_root=tmp_path
        )
        if placement == "visiting"
        else replace(_roaming(tmp_path / "worker.too"), instance="one")
    )
    monkeypatch.setattr(
        AgentProcess,
        "status",
        lambda self, **kwargs: AgentStatus(
            self.layout.name, "stopped", None, None, None, None
        ),
    )
    with ForegroundWorkload(temporary, toolang_root=tmp_path, drivers=frozenset()):
        snapshot = runtime_snapshot(tmp_path)
        assert snapshot.instances == ()
