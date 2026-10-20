from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from toolang.up.identity import allocate_roaming, select_roaming
from toolang.up.instance import read_instance, patch_workload
from toolang.up.observation import (
    RuntimeRegistration,
    instance_filename,
    runtime_inventory,
)


def test_parallel_allocations_are_isolated_and_keep_history(tmp_path, monkeypatch):
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    root = tmp_path / "global"
    monkeypatch.setattr("toolang.up.identity.time.time_ns", lambda: 100)
    with ThreadPoolExecutor(max_workers=4) as pool:
        layouts = list(
            pool.map(lambda _: allocate_roaming(source, toolang_root=root), range(12))
        )
    assert len({layout.root for layout in layouts}) == 12
    assert sorted(layout.request_order_ns for layout in layouts) == list(
        range(100, 112)
    )
    latest = max(layouts, key=lambda layout: layout.instance_id)
    assert select_roaming(source).root == latest.root
    for layout in layouts:
        assert layout.root.name == layout.instance_name
        assert layout.run_store == layout.home / ".runtime" / "runs.db"
        assert (
            layout.root_edit_lock
            == tmp_path / ".toolang" / ".state" / "root" / "edit.lock"
        )
        assert instance_filename(layout) == layout.instance_name + ".json"
        entry = read_instance(layout.runtime_status)
        assert entry is not None
        assert entry["control"]["source_path"] == str(source.resolve())
    source.unlink()
    assert select_roaming(source, latest.instance_id).root == latest.root


def test_registry_removal_and_failed_latest_do_not_change_selection(tmp_path):
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    root = tmp_path / "global"
    first = allocate_roaming(source, toolang_root=root)
    second = allocate_roaming(source, toolang_root=root)
    with RuntimeRegistration(
        first, toolang_root=root, drivers=frozenset()
    ) as registration:
        registration.path.unlink()
        assert select_roaming(source, first.instance_id).root == first.root
    entry = read_instance(second.runtime_status)
    assert entry is not None
    patch_workload(second.runtime_status, entry["launch_id"], {"status": "failed"})
    assert select_roaming(source).root == second.root
    assert first.runtime_status.exists()
    assert not runtime_inventory(root).snapshot.instances


def test_select_rejects_unknown_or_corrupt_identity(tmp_path):
    source = tmp_path / "aid.too"
    layout = allocate_roaming(source, toolang_root=tmp_path / "global")
    with pytest.raises(ValueError, match="requires r"):
        select_roaming(source, "abc")
    with pytest.raises(ValueError, match="invalid retained"):
        select_roaming(source, "r" + "f" * 32)
    layout.runtime_status.write_text("broken")
    with pytest.raises(ValueError):
        select_roaming(source)


def test_instance_identity_uses_canonical_path_even_with_the_same_clock_and_nonce(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("toolang.up.identity.time.time_ns", lambda: 100)
    monkeypatch.setattr("toolang.up.identity.uuid4", lambda: UUID(int=1))
    first = allocate_roaming(
        tmp_path / "one" / "aid.too", toolang_root=tmp_path / "global"
    )
    second = allocate_roaming(
        tmp_path / "two" / "aid.too", toolang_root=tmp_path / "global"
    )
    assert first.request_order_ns == second.request_order_ns
    assert first.instance_id != second.instance_id
    alias = tmp_path / "alias.too"
    alias.symlink_to(first.source_path)
    assert select_roaming(alias, first.instance_id).root == first.root
