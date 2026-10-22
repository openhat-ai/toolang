from toolang.up.registry import instance_filename
from toolang.up.workload import ForegroundWorkload
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from toolang.up.allocation import allocate_roaming, select_roaming
from toolang.up.instance import read_instance, patch_workload
from toolang.up.observation import (
    runtime_inventory,
)


def test_parallel_allocations_are_isolated_and_keep_history(tmp_path, monkeypatch):
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    root = tmp_path / "global"
    monkeypatch.setattr("toolang.up.allocation.time.time_ns", lambda: 100_000_000)
    with ThreadPoolExecutor(max_workers=4) as pool:
        layouts = list(
            pool.map(lambda _: allocate_roaming(source, toolang_root=root), range(12))
        )
    assert len({layout.place for layout in layouts}) == 12
    assert sorted(layout.request_order_ns for layout in layouts) == list(
        (value * 1_000_000 for value in range(100, 112))
    )
    latest = max(layouts, key=lambda layout: layout.place_code)
    assert select_roaming(source, toolang_root=root).place == latest.place
    for layout in layouts:
        assert layout.place.name == layout.place_code
        assert layout.run_store == layout.home / ".runtime" / "runs.db"
        assert (
            layout.place_edit_lock
            == tmp_path / ".toolang" / ".state" / "root" / "edit.lock"
        )
        assert instance_filename(layout) == layout.address + ".json"
        entry = read_instance(layout.runtime_status)
        assert entry is not None
        assert entry["control"]["source_path"] == str(source.resolve())
    source.unlink()
    assert (
        select_roaming(source, latest.place_code, toolang_root=root).place
        == latest.place
    )


def test_registry_removal_and_failed_latest_do_not_change_selection(tmp_path):
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    root = tmp_path / "global"
    first = allocate_roaming(source, toolang_root=root)
    second = allocate_roaming(source, toolang_root=root)
    with ForegroundWorkload(
        first, toolang_root=root, drivers=frozenset()
    ) as registration:
        registration.path.unlink()
        assert (
            select_roaming(source, first.place_code, toolang_root=root).place
            == first.place
        )
    entry = read_instance(second.runtime_status)
    assert entry is not None
    patch_workload(second.runtime_status, entry["launch_id"], {"status": "failed"})
    assert select_roaming(source, toolang_root=root).place == second.place
    assert first.runtime_status.exists()
    assert not runtime_inventory(root).snapshot.instances


def test_select_rejects_unknown_or_corrupt_identity(tmp_path):
    source = tmp_path / "aid.too"
    layout = allocate_roaming(source, toolang_root=tmp_path / "global")
    with pytest.raises(ValueError, match="place code requires"):
        select_roaming(source, "abc", toolang_root=tmp_path / "global")
    with pytest.raises(ValueError, match="invalid retained"):
        select_roaming(source, "s" + "0" * 17, toolang_root=tmp_path / "global")
    layout.runtime_status.write_text("broken")
    with pytest.raises(ValueError):
        select_roaming(source, toolang_root=tmp_path / "global")


def test_instance_identity_uses_canonical_path_even_with_the_same_clock_and_nonce(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("toolang.up.allocation.time.time_ns", lambda: 100_000_000)
    monkeypatch.setattr("toolang.up.allocation.uuid4", lambda: UUID(int=1))
    first = allocate_roaming(
        tmp_path / "one" / "aid.too", toolang_root=tmp_path / "global"
    )
    second = allocate_roaming(
        tmp_path / "two" / "aid.too", toolang_root=tmp_path / "global"
    )
    assert first.request_order_ns == second.request_order_ns
    assert first.place_code != second.place_code
    alias = tmp_path / "alias.too"
    alias.symlink_to(first.source_path)
    assert (
        select_roaming(alias, first.place_code, toolang_root=tmp_path / "global").place
        == first.place
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("root", "/other"), ("placement", "resident"), ("request_order_ns", -1)],
)
def test_retained_selection_rejects_inconsistent_identity_bindings(
    tmp_path, field, value
):
    from toolang.up.instance import update_instance

    source = tmp_path / "aid.too"
    layout = allocate_roaming(source, toolang_root=tmp_path / "global")
    update_instance(
        layout.runtime_status, lambda entry: entry["control"].update({field: value})
    )
    with pytest.raises(ValueError, match="identity"):
        select_roaming(source, layout.place_code, toolang_root=tmp_path / "global")


def test_registry_publication_failure_retains_a_failed_selectable_identity(
    tmp_path, monkeypatch
):
    from toolang.up.registry import RuntimeRegistration

    source = tmp_path / "aid.too"
    root = tmp_path / "global"

    def fail(_registration):
        raise PermissionError("registry is read-only")

    monkeypatch.setattr(RuntimeRegistration, "publish", fail)
    with pytest.raises(PermissionError, match="registry is read-only"):
        allocate_roaming(source, toolang_root=root)
    retained = select_roaming(source, toolang_root=root)
    entry = read_instance(retained.runtime_status)
    assert entry is not None
    assert entry["workload"]["status"] == "failed"
    assert entry["workload"]["pid"] is None
    assert "registry is read-only" in entry["workload"]["message"]
    assert not list((root / ".runtime" / "active").glob("*.json"))


def test_clock_rollback_and_other_sources_do_not_reorder_one_source(
    tmp_path, monkeypatch
):
    from toolang.common.place_codes import roaming_timestamp_ms

    now = [500_000_000]
    monkeypatch.setattr("toolang.up.allocation.time.time_ns", lambda: now[0])
    source = tmp_path / "aid.too"
    root = tmp_path / "global"
    first = allocate_roaming(source, toolang_root=root)
    now[0] = 900_000_000
    other = allocate_roaming(tmp_path / "other.too", toolang_root=root)
    now[0] = 100_000_000
    second = allocate_roaming(source, toolang_root=root)
    assert first.place_code is not None and second.place_code is not None
    assert other.place_code is not None
    assert roaming_timestamp_ms(first.place_code) == 500
    assert roaming_timestamp_ms(second.place_code) == 501
    assert roaming_timestamp_ms(other.place_code) == 900
    assert second.request_order_ns == 501_000_000
    first.runtime_status.touch()
    assert select_roaming(source, toolang_root=root).place == second.place


@pytest.mark.parametrize("scope", ["local", "registry"])
def test_roaming_full_id_collision_fails_once_and_preserves_owner(
    tmp_path, monkeypatch, scope
):
    from toolang.common import place_codes

    monkeypatch.setattr(place_codes, "_tag", lambda *_: 123)
    monkeypatch.setattr("toolang.up.allocation.time.time_ns", lambda: 100_000_000)
    root = tmp_path / "global"
    first_source = tmp_path / "one" / "a-id.too"
    second_source = (
        first_source.parent if scope == "local" else tmp_path / "two"
    ) / "a id.too"
    first = allocate_roaming(first_source, toolang_root=root)
    reference = root / ".runtime" / "active" / instance_filename(first)
    before = (first.runtime_status.read_bytes(), reference.read_bytes())
    with pytest.raises((ValueError, FileExistsError), match="collision"):
        allocate_roaming(second_source, toolang_root=root)
    assert (first.runtime_status.read_bytes(), reference.read_bytes()) == before
    candidates = list((second_source.parent / ".toolang").glob("*/s*"))
    assert candidates == ([first.place] if scope == "local" else [])
    assert list(reference.parent.glob("*.json")) == [reference]


@pytest.mark.parametrize("mutation", ["units", "path_hash", "format", "missing"])
def test_allocation_rejects_untrusted_retained_ownership_without_rewriting_it(
    tmp_path, mutation
):
    import json

    source = tmp_path / "aid.too"
    root = tmp_path / "global"
    retained = allocate_roaming(source, toolang_root=root)
    data = json.loads(retained.runtime_status.read_text())
    if mutation == "units":
        data["instances"]["default"]["control"]["request_order_ns"] //= 1_000_000
    elif mutation == "path_hash":
        data["instances"]["default"]["control"]["source_path"] = str(
            tmp_path / "foreign.too"
        )
    elif mutation == "format":
        data["version"] = 3
    else:
        retained.runtime_status.unlink()
    if mutation != "missing":
        retained.runtime_status.write_text(json.dumps(data))
    before = (
        retained.runtime_status.read_bytes()
        if retained.runtime_status.exists()
        else None
    )
    with pytest.raises(ValueError):
        allocate_roaming(source, toolang_root=root)
    assert (
        retained.runtime_status.read_bytes()
        if retained.runtime_status.exists()
        else None
    ) == before
    assert list((source.parent / ".toolang").glob("*/s*")) == [retained.place]


def test_timestamp_overflow_does_not_wrap_or_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "toolang.up.allocation.time.time_ns", lambda: (2**48 - 1) * 1_000_000
    )
    source = tmp_path / "aid.too"
    root = tmp_path / "global"
    retained = allocate_roaming(source, toolang_root=root)
    before = retained.runtime_status.read_bytes()
    with pytest.raises(ValueError, match="48-bit"):
        allocate_roaming(source, toolang_root=root)
    assert retained.runtime_status.read_bytes() == before
    assert list((source.parent / ".toolang").glob("*/s*")) == [retained.place]


def test_parallel_source_allocators_fence_a_global_full_id_collision(
    tmp_path, monkeypatch
):
    from toolang.common import place_codes

    monkeypatch.setattr(place_codes, "_tag", lambda *_: 123)
    monkeypatch.setattr("toolang.up.allocation.time.time_ns", lambda: 100_000_000)
    root = tmp_path / "global"

    def launch(source):
        try:
            return allocate_roaming(source, toolang_root=root)
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                launch, [tmp_path / "one" / "aid.too", tmp_path / "two" / "aid.too"]
            )
        )
    failures = [item for item in outcomes if isinstance(item, ValueError)]
    assert len(failures) == 1 and "collision" in str(failures[0])
    assert len(list((root / ".runtime" / "active").glob("*.json"))) == 1
    assert len(list(tmp_path.glob("*/.toolang/*/s*"))) == 1


def test_previous_layout_is_ignored_without_reading_or_modifying_it(tmp_path):
    import json

    source = tmp_path / "aid.too"
    old_root = tmp_path / ".toolang" / ("aid@r" + "a" * 32)
    status = old_root / "agents" / "aid" / ".runtime" / "status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"version": 3, "instances": {}}))
    before = status.read_bytes()
    root = tmp_path / "global"
    current = allocate_roaming(source, toolang_root=root)
    assert select_roaming(source, toolang_root=root).place == current.place
    assert current.place != old_root
    assert status.read_bytes() == before


def test_a_shared_path_tag_does_not_mix_retained_source_order_or_selection(
    tmp_path, monkeypatch
):
    from toolang.common import place_codes

    monkeypatch.setattr(place_codes, "_tag", lambda *_: 123)
    now = [500_000_000]
    monkeypatch.setattr("toolang.up.allocation.time.time_ns", lambda: now[0])
    root = tmp_path / "global"
    source = tmp_path / "a-id.too"
    first = allocate_roaming(source, toolang_root=root)
    now[0] = 900_000_000
    other = allocate_roaming(tmp_path / "a id.too", toolang_root=root)
    now[0] = 100_000_000
    second = allocate_roaming(source, toolang_root=root)
    assert first.request_order_ns == 500_000_000
    assert second.request_order_ns == 501_000_000
    assert other.request_order_ns == 900_000_000
    assert select_roaming(source, toolang_root=root).place == second.place
    assert other.source_path is not None
    assert select_roaming(other.source_path, toolang_root=root).place == other.place


def test_new_roaming_allocation_rejects_a_dangling_reference_to_its_candidate(
    tmp_path, monkeypatch
):
    from dataclasses import replace
    import json
    from toolang.common.place_codes import roaming_place_code
    from toolang.common.layout import AgentLayout

    source = tmp_path / "aid.too"
    root = tmp_path / "global"
    monkeypatch.setattr("toolang.up.allocation.time.time_ns", lambda: 100_000_000)
    candidate = replace(
        AgentLayout.roaming(
            source, place_code=roaming_place_code(str(source), 100), root=root
        ),
        root=root,
    )
    reference = root / ".runtime" / "active" / instance_filename(candidate)
    reference.parent.mkdir(parents=True)
    reference.write_text(
        json.dumps(
            {
                "version": 5,
                "status": str(candidate.runtime_status),
                "launch_id": UUID(int=1).hex,
            }
        )
    )
    before = reference.read_bytes()
    with pytest.raises(ValueError, match="collision"):
        allocate_roaming(source, toolang_root=root)
    assert reference.read_bytes() == before
    assert not candidate.place.exists()


@pytest.mark.parametrize(
    "field", ["runtime_instance", "source_path", "source_ref", "request_order_ns"]
)
def test_missing_identity_fields_raise_a_protocol_error_instead_of_key_error(
    tmp_path, field
):
    from toolang.common.layout import AgentLayout
    from toolang.up.identity import identity_bindings, layout_from_control

    control = identity_bindings(AgentLayout.resident(tmp_path, "alice"))
    control.pop(field)
    with pytest.raises(ValueError, match="identity"):
        layout_from_control(control)


@pytest.mark.parametrize(
    "runtime_instance", [1, True, {"invalid": "value"}, ["invalid"]]
)
def test_invalid_runtime_instance_types_raise_a_protocol_error(
    tmp_path, runtime_instance
):
    from toolang.common.layout import AgentLayout
    from toolang.up.identity import identity_bindings, layout_from_control

    control = identity_bindings(AgentLayout.resident(tmp_path, "alice"))
    control["runtime_instance"] = runtime_instance
    with pytest.raises(ValueError, match="identity"):
        layout_from_control(control)


@pytest.mark.parametrize("component", ["cache", "state", "guard"])
def test_roaming_allocation_rejects_linked_cache_guards_before_any_foreign_write(
    tmp_path, component
):
    source = tmp_path / "project" / "aid.too"
    source.parent.mkdir()
    cache = source.parent / ".toolang"
    peer = tmp_path / "peer"
    peer.mkdir()
    marker = peer / "keep"
    marker.write_text("peer data")
    if component == "cache":
        cache.symlink_to(peer, target_is_directory=True)
    else:
        cache.mkdir()
        if component == "state":
            (cache / ".state").symlink_to(peer, target_is_directory=True)
        else:
            (cache / ".state").mkdir()
            (cache / ".state" / "launch.lock").symlink_to(peer / "other.lock")
    with pytest.raises((OSError, ValueError)):
        allocate_roaming(source, toolang_root=tmp_path / "global")
    assert set(peer.iterdir()) == {marker}
    assert marker.read_text() == "peer data"
    assert not (tmp_path / "global" / ".runtime" / "active").exists()


@pytest.mark.parametrize("guard_already_exists", [False, True])
def test_retained_selection_waits_for_initial_status_publication(
    tmp_path, monkeypatch, guard_already_exists
):
    from contextlib import contextmanager
    from threading import Event, current_thread
    from toolang.common import files
    from toolang.up import allocation, workload

    source = tmp_path / "aid.too"
    root = tmp_path / "global"
    launch_guard = tmp_path / ".toolang" / ".state" / "launch.lock"
    preparing = Event()
    selector_reached_boundary = Event()
    finish_publication = Event()
    original_write = workload.atomic_write_text
    original_read = allocation.read_instance
    original_lock = files.file_write_lock
    original_select = allocation._select_retained_roaming

    def paused_write(path, content, **kwargs):
        if path.name == "status.json":
            preparing.set()
            assert finish_publication.wait(5)
        return original_write(path, content, **kwargs)

    def observed_read(path, *args, **kwargs):
        result = original_read(path, *args, **kwargs)
        if current_thread().name.startswith("selector") and result is None:
            selector_reached_boundary.set()
        return result

    @contextmanager
    def observed_lock(path, **kwargs):
        if path == launch_guard and current_thread().name.startswith("selector"):
            selector_reached_boundary.set()
        with original_lock(path, **kwargs):
            yield

    monkeypatch.setattr(workload, "atomic_write_text", paused_write)
    monkeypatch.setattr(allocation, "read_instance", observed_read)
    monkeypatch.setattr(files, "file_write_lock", observed_lock)
    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="allocator") as allocator,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="selector") as selector,
    ):
        allocations = []

        def start_allocation_then_select(source, place_code, toolang_root):
            if not allocations:
                allocations.append(
                    allocator.submit(allocate_roaming, source, toolang_root=root)
                )
                assert preparing.wait(5)
            return original_select(source, place_code, toolang_root)

        monkeypatch.setattr(
            allocation, "_select_retained_roaming", start_allocation_then_select
        )
        try:
            if guard_already_exists:
                allocations.append(
                    allocator.submit(allocate_roaming, source, toolang_root=root)
                )
                assert preparing.wait(5)
            else:
                assert not launch_guard.exists()
            selection = selector.submit(select_roaming, source, toolang_root=root)
            assert selector_reached_boundary.wait(5)
        finally:
            finish_publication.set()
        published = allocations[0].result(timeout=5)
        selected = selection.result(timeout=5)
    assert selected.place == published.place


def test_retained_selection_does_not_recreate_a_missing_source_guard(tmp_path):
    source = tmp_path / "aid.too"
    layout = allocate_roaming(source, toolang_root=tmp_path / "global")
    guard = tmp_path / ".toolang" / ".state" / "launch.lock"
    guard.unlink()
    before = layout.runtime_status.read_bytes()
    assert (
        select_roaming(source, toolang_root=tmp_path / "global").place == layout.place
    )
    assert not guard.exists()
    assert layout.runtime_status.read_bytes() == before
