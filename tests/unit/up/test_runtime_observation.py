from dataclasses import replace
import asyncio
import hashlib
import json
import os

import pytest

from toolang.common.layout import AgentLayout
from toolang.execution.store import RunStore
from toolang.up.observation import (
    RuntimeRegistration,
    instance_filename,
    running_instances,
    runtime_snapshot,
)


def test_reference_names_use_stable_host_source_identity(tmp_path):
    root = tmp_path / "toolang"
    resident = AgentLayout.resident(root, "gus")
    source = "https://example.com/lex.too"
    visiting = AgentLayout.visiting(source, "lex", toolang_root=root)
    visiting_hash = hashlib.sha256(f"{root.resolve()}\0{source}".encode()).hexdigest()
    roaming = AgentLayout.roaming(tmp_path / "aid.too")
    roaming_hash = hashlib.sha256(
        str((tmp_path / "aid.too").resolve()).encode()
    ).hexdigest()

    assert instance_filename(resident) == "gus.json"
    assert instance_filename(visiting) == f"lex@v{visiting_hash[:16]}.json"
    assert visiting.root.name == f"toolang-lex-{visiting_hash[:16]}"
    assert visiting.source_id == visiting_hash[:16]
    assert instance_filename(roaming) == f"aid@r{roaming_hash[:16]}.json"
    assert instance_filename(replace(roaming)) == instance_filename(roaming)
    assert instance_filename(
        AgentLayout.roaming(tmp_path / "other" / "aid.too")
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
    assert instance_filename(AgentLayout.roaming(alias)) == instance_filename(
        AgentLayout.roaming(source)
    )
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
    from toolang.up.records import read_record

    layout = (
        AgentLayout.visiting(
            "https://example.com/lex.too", "lex", toolang_root=tmp_path
        )
        if placement == "visiting"
        else AgentLayout.roaming(tmp_path / "aid.too")
    )
    old = RuntimeRegistration(layout, toolang_root=tmp_path, drivers=frozenset())
    current = RuntimeRegistration(layout, toolang_root=tmp_path, drivers=frozenset())
    with old, current:
        assert old.path == current.path
        assert old.token != current.token
        record = read_record(layout.runtime_status)
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
    registration = RuntimeRegistration(
        layout, toolang_root=tmp_path, drivers=frozenset()
    )
    registration.__enter__()
    for value in (None, [], "invalid", 123):
        registration.path.write_text(json.dumps(value))
        assert running_instances(tmp_path) == ()
        registration.close()


def test_discovery_does_not_adopt_previous_reference_names(tmp_path):
    layout = AgentLayout.resident(tmp_path, "gus")
    with RuntimeRegistration(
        layout, toolang_root=tmp_path, drivers=frozenset()
    ) as registration:
        old_path = registration.path.with_name("gus-resident-0123456789abcdef.json")
        registration.path.rename(old_path)
        assert running_instances(tmp_path) == ()


@pytest.mark.parametrize("instances", [None, [], "invalid", 123])
def test_discovery_ignores_invalid_status_instances(tmp_path, instances):
    layout = AgentLayout.resident(tmp_path, "gus")
    with RuntimeRegistration(layout, toolang_root=tmp_path, drivers=frozenset()):
        before = layout.runtime_status.read_bytes()
        try:
            layout.runtime_status.write_text(
                json.dumps({"version": 2, "instances": instances})
            )
            assert running_instances(tmp_path) == ()
        finally:
            layout.runtime_status.write_bytes(before)


@pytest.mark.parametrize("invalid", [None, [], "invalid", {"launch_id": "bad"}])
def test_discovery_preserves_valid_entries_beside_a_malformed_peer(tmp_path, invalid):
    layout = AgentLayout.resident(tmp_path, "gus")
    with RuntimeRegistration(layout, toolang_root=tmp_path, drivers=frozenset()):
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
    with RuntimeRegistration(
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
    one = replace(AgentLayout.roaming(source), instance="one")
    two = replace(one, instance="two")
    assert one.home == two.home
    assert one.run_store != two.run_store
    assert one.chore_store == one.run_store
    assert one.chore_store != two.chore_store
    assert one.runtime_status == two.runtime_status
    store = RunStore(one.run_store)
    store.create_thread(thread_id="talk_existing", origin="talk")
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
    old = RuntimeRegistration(layout, toolang_root=tmp_path, drivers=frozenset())
    current = RuntimeRegistration(
        layout, toolang_root=tmp_path, drivers=frozenset(("chat",))
    )
    old.__enter__()
    current.__enter__()
    old.close()
    assert running_instances(tmp_path)[0]["drivers"] == ["chat"]
    assert runtime_snapshot(tmp_path).instances[0].drivers == ("chat",)
    from toolang.up.records import patch_workload

    patch_workload(layout.runtime_status, current.token, {"process_created": 0})
    assert running_instances(tmp_path) == ()
    current.close()


def test_guest_discovery_uses_host_sandbox_ownership_not_guest_pid(
    tmp_path, monkeypatch
):
    from toolang.base.types.sandbox import SandboxRef
    from toolang.up import sandbox
    from toolang.up.records import SandboxState
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
    with RuntimeRegistration(
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
        from toolang.up.records import patch_workload

        patch_workload(
            layout.runtime_status, registration.token, {"updated_at": "later"}
        )
        assert running_instances(tmp_path) == ()


def test_unbound_guest_discovery_preserves_unknown_workload(tmp_path):
    from toolang.up.records import patch_workload, update_record

    layout = AgentLayout.resident(tmp_path, "guest")
    with RuntimeRegistration(
        layout, toolang_root=tmp_path, drivers=frozenset()
    ) as registration:
        update_record(
            layout.runtime_status,
            lambda entry: entry["control"].update(sandbox="docker:image"),
        )
        patch_workload(
            layout.runtime_status, registration.token, {"process_created": 0}
        )
        before = layout.runtime_status.read_bytes()
        assert running_instances(tmp_path) == ()
        assert layout.runtime_status.read_bytes() == before


def test_short_lived_scripts_share_one_register_lock_and_readable_references(tmp_path):
    scope = tmp_path / "toolang"
    layout = AgentLayout.roaming(tmp_path / "echo.too")
    directory = scope / ".runtime" / "instances"
    assert running_instances(scope) == ()
    assert not directory.exists()
    for index in range(20):
        with RuntimeRegistration(
            replace(layout, instance=f"script{index}"),
            toolang_root=scope,
            drivers=frozenset(),
        ) as registration:
            assert registration.path.name.startswith(f"echo@r{layout.source_id}@")
            assert registration.path.name.endswith(f"@script{index}.json")
    assert tuple(path.name for path in directory.iterdir()) == ("register.lock",)
    from toolang.up.records import read_document

    document = read_document(layout.runtime_status)
    assert document is not None and document["instances"] == {}


@pytest.mark.parametrize("legacy_guard", ["update.lock", None])
def test_cleanup_creates_register_guard_for_an_existing_reference(
    tmp_path, legacy_guard
):
    from toolang.up.process import stop_runtime_state

    layout = AgentLayout.resident(tmp_path, "gus")
    registration = RuntimeRegistration(
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
    from toolang.up.records import update_record

    layout = AgentLayout.resident(tmp_path, "gus")
    old = RuntimeRegistration(layout, toolang_root=tmp_path, drivers=frozenset())
    old.__enter__()
    guard = old.path.parent / "register.lock"
    current = RuntimeRegistration(
        layout, toolang_root=tmp_path, drivers=frozenset(), launch_id="new"
    )
    entered = Event()

    def close_old():
        entered.set()
        old.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_write_lock(guard):
            closing = pool.submit(close_old)
            assert entered.wait(timeout=1)
            assert not closing.done()
            update_record(
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
    from toolang.up.records import SandboxState
    from toolang.up.sandbox import _clear_state

    layout = (
        AgentLayout.visiting(
            "https://example.com/alice.too", "alice", toolang_root=tmp_path
        )
        if placement == "visiting"
        else AgentLayout.roaming(tmp_path / "alice.too")
    )
    with RuntimeRegistration(
        layout, toolang_root=tmp_path / "toolang", drivers=frozenset()
    ) as registration:
        ref = SandboxState(
            "host",
            process_ref(os.getpid(), "http://localhost:8123"),
            registration.token,
        )
        ref.save(layout.runtime_status)
        runtime_layout = replace(layout, placement="resident", source_id=None)
        _clear_state(runtime_layout, expected=ref)
        assert not registration.path.exists()


def test_discovery_reads_a_shared_agent_document_once_per_refresh(
    tmp_path, monkeypatch
):
    from toolang.up import observation, records
    from contextlib import ExitStack

    layout = AgentLayout.roaming(tmp_path / "echo.too")
    with ExitStack() as scope:
        for index in range(10):
            scope.enter_context(
                RuntimeRegistration(
                    replace(layout, instance=f"script{index}"),
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
        assert reads == 1


def test_agent_removal_retains_home_used_by_an_independent_script(tmp_path):
    from toolang.up.sandbox import remove_agent

    layout = AgentLayout.resident(tmp_path, "alice")
    with RuntimeRegistration(
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
    roaming = AgentLayout.roaming(tmp_path / "alice.too")

    with (
        RuntimeRegistration(
            resident, toolang_root=root, drivers=frozenset({"chat", "talk"})
        ),
        RuntimeRegistration(visiting, toolang_root=root, drivers=frozenset({"chat"})),
        RuntimeRegistration(roaming, toolang_root=root, drivers=frozenset()),
    ):
        snapshot = runtime_snapshot(root)

    assert not snapshot.partial
    assert len(snapshot.instances) == 4
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
    stopped = next(item for item in snapshot.instances if item.name == "bob")
    assert stopped.state == "not running" and stopped.drivers is None


def test_runtime_snapshot_reports_starting_and_invalid_drivers_as_partial(tmp_path):
    from toolang.up.records import update_record

    root = tmp_path / "toolang"
    layout = AgentLayout.resident(root, "alice")
    layout.home.mkdir(parents=True)
    with RuntimeRegistration(
        layout, toolang_root=root, drivers=frozenset({"chat", "talk"})
    ) as registration:
        update_record(
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

        update_record(
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
    with RuntimeRegistration(layout, toolang_root=root, drivers=frozenset({"talk"})):
        original = observation.read_document
        reads = []

        def counted(path, *, location="host"):
            reads.append(path)
            return original(path, location=location)

        monkeypatch.setattr(observation, "read_document", counted)
        snapshot = runtime_snapshot(root)
        assert len(snapshot.instances) == 1
        assert reads.count(layout.runtime_status.resolve()) == 1


def test_runtime_snapshot_keeps_valid_residents_beside_malformed_peer(tmp_path):
    valid = AgentLayout.resident(tmp_path, "alice")
    valid.home.mkdir(parents=True)
    malformed = AgentLayout.resident(tmp_path, "bob")
    malformed.home.mkdir(parents=True)
    malformed.runtime_status.parent.mkdir(parents=True, exist_ok=True)
    malformed.runtime_status.write_text("not json")

    snapshot = runtime_snapshot(tmp_path)

    alice = next(item for item in snapshot.instances if item.name == "alice")
    bob = next(item for item in snapshot.instances if item.name == "bob")
    assert alice.state == "not running"
    assert bob.state == "unknown" and bob.diagnostic is not None
    assert snapshot.partial


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
        registration = RuntimeRegistration(
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
    from toolang.up.records import SandboxState

    root = tmp_path / "toolang"
    registrations = []
    for index in range(3):
        layout = AgentLayout.resident(root, f"agent{index}")
        layout.home.mkdir(parents=True)
        registration = RuntimeRegistration(
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


def test_runtime_snapshot_preserves_distinct_temporary_instance_labels(tmp_path):
    from dataclasses import replace

    root = tmp_path / "toolang"
    base = AgentLayout.visiting(
        "https://example.com/alice.too", "alice", toolang_root=root
    )
    one = replace(base, instance="one")
    two = replace(base, instance="two")
    with (
        RuntimeRegistration(one, toolang_root=root, drivers=frozenset({"talk"})),
        RuntimeRegistration(two, toolang_root=root, drivers=frozenset({"talk"})),
    ):
        snapshot = runtime_snapshot(root)

    assert len(snapshot.instances) == 2
    assert {item.runtime_instance for item in snapshot.instances} == {"one", "two"}
    assert len({item.label for item in snapshot.instances}) == 2
    assert all("@v" in item.label and "#" in item.label for item in snapshot.instances)


@pytest.mark.parametrize("terminal_state", ["stopped", "failed"])
def test_runtime_snapshot_keeps_failed_residents_and_excludes_finished_visitors(
    tmp_path, terminal_state
):
    from toolang.up.records import update_record

    root = tmp_path / "toolang"
    resident = AgentLayout.resident(root, "alice")
    resident.home.mkdir(parents=True)
    visiting = AgentLayout.visiting(
        "https://example.com/visitor.too", "visitor", toolang_root=root
    )
    with (
        RuntimeRegistration(
            resident, toolang_root=root, drivers=frozenset({"talk"})
        ) as resident_run,
        RuntimeRegistration(
            visiting, toolang_root=root, drivers=frozenset({"talk"})
        ) as visiting_run,
    ):
        for layout, registration in (
            (resident, resident_run),
            (visiting, visiting_run),
        ):
            update_record(
                layout.runtime_status,
                lambda entry: entry["workload"].update(status=terminal_state, pid=None),
                instance=layout.instance,
                launch_id=registration.token,
            )
        snapshot = runtime_snapshot(root)

    assert len(snapshot.instances) == 1
    assert snapshot.instances[0].name == "alice"
    assert snapshot.instances[0].state == "not running"
    assert snapshot.instances[0].drivers is None


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
        else replace(AgentLayout.roaming(tmp_path / "worker.too"), instance="one")
    )
    monkeypatch.setattr(
        AgentProcess,
        "status",
        lambda self, **kwargs: AgentStatus(
            self.layout.name, "stopped", None, None, None, None
        ),
    )
    with RuntimeRegistration(temporary, toolang_root=tmp_path, drivers=frozenset()):
        snapshot = runtime_snapshot(tmp_path)
        assert len(snapshot.instances) == 1
        assert snapshot.instances[0].placement == "resident"
        assert snapshot.instances[0].state == "not running"
