"""Instance selectors bind one root and remain independent of source acquisition."""

import json

import pytest

from toolang.common.instance_ids import area_id
from toolang.common.layout import AgentLayout
from toolang.up.addresses import parse_address, resolve_address
from toolang.up.identity import allocate_roaming, select_roaming, visiting_home_guard
from toolang.up.workload import fail_preparation


def test_roaming_path_survives_exit_and_source_removal_but_short_name_does_not(
    tmp_path,
):
    root = tmp_path / "root"
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    layout = allocate_roaming(source, toolang_root=root)
    assert (
        layout.root == tmp_path / ".toolang" / area_id(str(root)) / layout.instance_id
    )
    short = parse_address(layout.instance_name)
    path = parse_address(f"{source}@{layout.instance_id}")
    assert short is not None and path is not None
    assert resolve_address(root, short)[0] == layout
    assert resolve_address(root, path)[0] == layout
    fail_preparation(layout, RuntimeError("finished"))
    source.unlink()
    assert resolve_address(root, path)[0] == layout
    with pytest.raises(FileNotFoundError, match="source path for offline"):
        resolve_address(root, short)
    assert not list((root / ".runtime" / "active").glob("*.json"))


def test_one_source_has_separate_toolang_areas_and_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("toolang.up.identity.time.time_ns", lambda: 100_000_000)
    source = tmp_path / "aid.too"
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first = allocate_roaming(source, toolang_root=first_root)
    second = allocate_roaming(source, toolang_root=second_root)
    assert first.instance_id == second.instance_id
    assert first.root != second.root
    assert select_roaming(source, toolang_root=first_root).root == first.root
    assert select_roaming(source, toolang_root=second_root).root == second.root
    for layout, root in ((first, first_root), (second, second_root)):
        assert json.loads((layout.root.parent / "root.json").read_text()) == {
            "toolang_root": str(root)
        }


def test_explicit_path_rejects_an_area_hash_collision(tmp_path, monkeypatch):
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    shared_area = area_id(str(first_root))
    for module in ("common.layout", "up.identity", "up.private_paths"):
        monkeypatch.setattr(f"toolang.{module}.area_id", lambda _root: shared_area)
    source = tmp_path / "aid.too"
    layout = allocate_roaming(source, toolang_root=first_root)
    selector = parse_address(f"{source}@{layout.instance_id}")
    assert selector is not None
    before = layout.runtime_status.read_bytes()
    with pytest.raises(ValueError, match="area ID collision"):
        resolve_address(second_root, selector)
    assert layout.runtime_status.read_bytes() == before
    assert not second_root.exists()


def test_visiting_name_uses_known_path_without_active_registration(
    tmp_path, isolated_visiting_root
):
    root = tmp_path / "root"
    layout = AgentLayout.visiting("gh:example/lex", "lex", toolang_root=root)
    with visiting_home_guard(layout):
        pass
    selector = parse_address(layout.instance_name)
    assert selector is not None
    assert resolve_address(root, selector)[0] == layout
    assert not (root / ".runtime").exists()
    assert (
        layout.root == isolated_visiting_root / area_id(str(root)) / layout.instance_id
    )


@pytest.mark.parametrize(
    "text",
    [
        "gh:example/lex@main",
        "https://example.com/lex.too?user=a@somewhere",
        "dir/aid@work.too",
    ],
)
def test_source_at_characters_are_not_instance_suffixes(text):
    assert parse_address(text) is None


@pytest.mark.parametrize(
    "text", ["aid@r0000000000000000", "aid@s000", "aid@h000", "aid@S00000000000000000"]
)
def test_partial_and_previous_ids_are_rejected(text):
    with pytest.raises(ValueError):
        parse_address(text)


def test_empty_unpublished_candidate_is_skipped_without_overwriting_it(
    tmp_path, monkeypatch
):
    from toolang.common.instance_ids import roaming_instance_id

    monkeypatch.setattr("toolang.up.identity.time.time_ns", lambda: 100_000_000)
    source = tmp_path / "aid.too"
    root = tmp_path / "root"
    first = allocate_roaming(source, toolang_root=root)
    occupied = first.root.parent / roaming_instance_id(str(source), 101)
    occupied.mkdir(mode=0o700)
    second = allocate_roaming(source, toolang_root=root)
    assert second.request_order_ns == 102_000_000
    assert occupied.is_dir() and not tuple(occupied.iterdir())
    assert select_roaming(source, toolang_root=root).root == second.root


def test_retained_selection_rejects_a_linked_store_without_touching_its_target(
    tmp_path,
):
    source = tmp_path / "aid.too"
    root = tmp_path / "root"
    layout = allocate_roaming(source, toolang_root=root)
    target = tmp_path / "keep"
    target.write_text("unrelated data")
    layout.run_store.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        select_roaming(source, toolang_root=root)
    assert target.read_text() == "unrelated data"


def test_missing_area_ownership_is_not_recreated_over_retained_data(tmp_path):
    root = tmp_path / "root"
    source = tmp_path / "aid.too"
    layout = allocate_roaming(source, toolang_root=root)
    ownership = layout.root.parent / "root.json"
    ownership.unlink()
    before = layout.runtime_status.read_bytes()
    with pytest.raises(ValueError, match="unbound area"):
        allocate_roaming(source, toolang_root=root)
    assert not ownership.exists()
    assert layout.runtime_status.read_bytes() == before
