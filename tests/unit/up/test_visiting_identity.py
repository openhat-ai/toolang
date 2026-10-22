"""Visiting ownership is checked before source or history can be mutated."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat

import pytest

from toolang.common.files import EditLock, atomic_write_text
from toolang.common.layout import AgentLayout
from toolang.up.identity import visiting_home_guard
from toolang.up.instance import read_instance
from toolang.up.private_paths import _validate_directory
from toolang.up.source import HttpRef, materialize_visiting_program, visiting_layout


def _layout(root, url="https://example.com/alice.too"):
    return AgentLayout.visiting(url, "alice", toolang_root=root)


def test_deriving_a_visiting_layout_does_not_claim_or_create_a_home(
    tmp_path, isolated_visiting_root
):
    layout = visiting_layout("github:owner/alice", toolang_root=tmp_path)
    alias = visiting_layout("gh:owner/alice", toolang_root=tmp_path)
    assert layout == alias
    assert not layout.root.exists()
    assert not isolated_visiting_root.exists()


def test_parallel_claims_share_one_fully_bound_private_home(
    tmp_path, isolated_visiting_root
):
    layout = _layout(tmp_path)

    def claim(_):
        with visiting_home_guard(layout):
            entry = read_instance(layout.runtime_status)
            assert entry is not None
            assert entry["control"]["source_ref"] == layout.source_ref
            return entry["launch_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(claim, range(12)))) == 1
    for path in (isolated_visiting_root, layout.root, layout.home, layout.runtime):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE(layout.runtime_status.stat().st_mode) == 0o600


@pytest.mark.parametrize("version", [3, 4])
def test_unbound_or_unsupported_existing_homes_are_preserved(tmp_path, version):
    layout = _layout(tmp_path)
    with visiting_home_guard(layout):
        pass
    document = json.loads(layout.runtime_status.read_text())
    document["version"] = version
    document["instances"] = {}
    layout.runtime_status.write_text(json.dumps(document))
    before = layout.runtime_status.read_bytes()
    with pytest.raises(ValueError):
        materialize_visiting_program(
            HttpRef(layout.source_ref), "new", toolang_root=tmp_path
        )
    assert layout.runtime_status.read_bytes() == before
    assert not layout.program.exists()


def test_full_visiting_id_collision_fails_before_cached_program_or_history_changes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "toolang.common.layout.visiting_instance_id", lambda *_: "h" + "0" * 10
    )
    first = materialize_visiting_program(
        HttpRef("https://one.example/alice.too"), "original", toolang_root=tmp_path
    )
    atomic_write_text(first.run_store, "retained history")
    before = {
        path: path.read_bytes()
        for path in (
            first.program,
            first.runtime_status,
            first.run_store,
            first.home / "agent.source.json",
        )
    }
    with pytest.raises(ValueError, match="collision"):
        materialize_visiting_program(
            HttpRef("https://other.example/alice.too"),
            "replacement",
            toolang_root=tmp_path,
        )
    assert all(path.read_bytes() == value for path, value in before.items())


@pytest.mark.parametrize("mode", [0o755, 0o777, 0o1777])
def test_untrusted_parent_modes_fail_without_adoption(
    tmp_path, isolated_visiting_root, mode
):
    if mode == 0o1777 and os.geteuid() == 0:
        pytest.skip("root owns the accepted shared-parent mode")
    isolated_visiting_root.mkdir()
    isolated_visiting_root.chmod(mode)
    before = isolated_visiting_root.stat()
    with pytest.raises(ValueError, match="unsafe visiting directory"):
        with visiting_home_guard(_layout(tmp_path)):
            pytest.fail("untrusted parent was adopted")
    assert stat.S_IMODE(isolated_visiting_root.stat().st_mode) == stat.S_IMODE(
        before.st_mode
    )
    assert not list(isolated_visiting_root.iterdir())


@pytest.mark.parametrize(
    "owner,mode,accepted",
    [
        (0, 0o1777, True),
        (0, 0o777, False),
        (12345, 0o1777, False),
        (12345, 0o700, True),
        (12345, 0o755, False),
    ],
)
def test_parent_ownership_contract(owner, mode, accepted, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 12345)
    info = os.stat_result((stat.S_IFDIR | mode, 0, 0, 1, owner, 0, 0, 0, 0, 0))
    if accepted:
        _validate_directory(Path("/tmp/toolang"), info, shared=True)
    else:
        with pytest.raises(ValueError, match="owner or mode"):
            _validate_directory(Path("/tmp/toolang"), info, shared=True)


@pytest.mark.parametrize("component", ["parent", "root", "agents", "status", "program"])
def test_visiting_paths_reject_symlinks_without_touching_the_target(
    tmp_path, isolated_visiting_root, component
):
    layout = _layout(tmp_path)
    peer = tmp_path / "peer"
    peer.mkdir(mode=0o700)
    marker = peer / "keep"
    marker.write_text("peer data")
    if component == "parent":
        isolated_visiting_root.symlink_to(peer, target_is_directory=True)
    elif component == "root":
        layout.root.parent.mkdir(mode=0o700, parents=True)
        layout.root.symlink_to(peer, target_is_directory=True)
    else:
        with visiting_home_guard(layout):
            pass
        target = {
            "agents": layout.root / "agents",
            "status": layout.runtime_status,
            "program": layout.program,
        }[component]
        if target.exists():
            target.rename(target.with_name(target.name + ".saved"))
        target.symlink_to(peer if component == "agents" else marker)
    with pytest.raises((OSError, ValueError)):
        with visiting_home_guard(layout):
            pytest.fail("symlink was followed")
    assert marker.read_text() == "peer data"
    assert set(peer.iterdir()) == {marker}


def test_recheck_rejects_a_root_replaced_while_waiting_for_the_home_guard(
    tmp_path, monkeypatch
):
    layout = _layout(tmp_path)
    with visiting_home_guard(layout):
        pass
    peer = tmp_path / "peer"
    peer.mkdir(mode=0o700)
    original = EditLock.write

    @contextmanager
    def replaced(lock):
        with original(lock):
            layout.root.rename(layout.root.with_name("retained"))
            layout.root.symlink_to(peer, target_is_directory=True)
            yield

    monkeypatch.setattr(EditLock, "write", replaced)
    with pytest.raises(ValueError, match="changed"):
        with visiting_home_guard(layout):
            pytest.fail("replaced root was adopted")
    assert not list(peer.iterdir())


def test_management_claims_source_ownership_before_creating_lifecycle_metadata(
    tmp_path,
):
    from toolang.up.instance import management_operation

    layout = _layout(tmp_path)
    assert not layout.root.exists()
    with management_operation(layout, stage="start"):
        entry = read_instance(layout.runtime_status)
        assert entry is not None
        assert entry["control"]["source_ref"] == layout.source_ref
        assert entry["control"]["instance_id"] == layout.instance_id
        assert entry["control"]["operation"]["stage"] == "start"
    assert stat.S_IMODE(layout.root.stat().st_mode) == 0o700


def test_admin_shared_parent_still_creates_private_user_owned_instances(
    tmp_path, isolated_visiting_root, monkeypatch
):
    from toolang.up import private_paths

    isolated_visiting_root.mkdir()
    isolated_visiting_root.chmod(0o1777)
    parent_inode = isolated_visiting_root.stat().st_ino
    original_fstat = os.fstat
    original_lstat = Path.lstat

    def administrator_owned(info):
        if info.st_ino != parent_inode:
            return info
        fields = list(info)
        fields[4] = 0
        return os.stat_result(fields)

    # Provisioning root ownership is unavailable to unprivileged test runners.
    # Exercise the real directory-relative access with only that stat field faked.
    monkeypatch.setattr(
        private_paths.os,
        "fstat",
        lambda descriptor: administrator_owned(original_fstat(descriptor)),
    )
    monkeypatch.setattr(
        Path, "lstat", lambda path: administrator_owned(original_lstat(path))
    )
    layout = _layout(tmp_path)
    with visiting_home_guard(layout):
        pass
    assert stat.S_IMODE(layout.root.stat().st_mode) == 0o700
    assert layout.root.stat().st_uid == os.geteuid()
    assert stat.S_IMODE(isolated_visiting_root.stat().st_mode) == 0o1777
