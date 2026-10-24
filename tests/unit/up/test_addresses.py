"""Instance selectors bind one root and remain independent of source acquisition."""

import json

import pytest

from toolang.common.place_codes import PLACE_CODE_EPOCH_MS, root_code
from toolang.common.layout import AgentLayout
from toolang.up.addresses import parse_address, resolve_address
from toolang.up.allocation import allocate_roaming, select_roaming
from toolang.up.identity import visiting_home_guard
from toolang.up.workload import fail_preparation


def test_roaming_path_survives_exit_and_source_removal_but_short_name_does_not(
    tmp_path,
):
    root = tmp_path / "root"
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    layout = allocate_roaming(source, toolang_root=root)
    assert (
        layout.place == tmp_path / ".toolang" / root_code(str(root)) / layout.place_code
    )
    assert layout.root == root.resolve()
    assert layout.home == layout.place / "agents" / "aid"
    short = parse_address(layout.address)
    path = parse_address(f"{source}@{layout.place_code}")
    assert short is not None and path is not None
    assert resolve_address(root, short)[0] == layout
    assert resolve_address(root, path)[0] == layout
    fail_preparation(layout, RuntimeError("finished"))
    source.unlink()
    assert resolve_address(root, path)[0] == layout
    with pytest.raises(FileNotFoundError, match="source path for offline"):
        resolve_address(root, short)
    assert not list((root / ".runtime" / "active").glob("*/*.json"))


def test_one_source_has_separate_root_namespaces_and_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "toolang.up.allocation.time.time_ns",
        lambda: (PLACE_CODE_EPOCH_MS + 100) * 1_000_000,
    )
    source = tmp_path / "aid.too"
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first = allocate_roaming(source, toolang_root=first_root)
    second = allocate_roaming(source, toolang_root=second_root)
    assert first.place_code == second.place_code
    assert first.place != second.place
    assert select_roaming(source, toolang_root=first_root).place == first.place
    assert select_roaming(source, toolang_root=second_root).place == second.place
    for layout, root in ((first, first_root), (second, second_root)):
        assert json.loads((layout.place.parent / "root.json").read_text()) == {
            "root": str(root)
        }


def test_explicit_path_rejects_a_root_code_collision(tmp_path, monkeypatch):
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    shared_root_code = root_code(str(first_root))
    for module in ("common.layout", "up.allocation", "up.identity", "up.private_paths"):
        monkeypatch.setattr(
            f"toolang.{module}.root_code", lambda _root: shared_root_code
        )
    source = tmp_path / "aid.too"
    layout = allocate_roaming(source, toolang_root=first_root)
    selector = parse_address(f"{source}@{layout.place_code}")
    assert selector is not None
    before = layout.runtime_status.read_bytes()
    with pytest.raises(ValueError, match="root code collision"):
        resolve_address(second_root, selector)
    assert layout.runtime_status.read_bytes() == before
    assert not second_root.exists()


def test_visiting_name_uses_known_path_without_active_registration(
    tmp_path, isolated_visiting_base
):
    root = tmp_path / "root"
    layout = AgentLayout.visiting("gh:example/lex", "lex", root=root)
    with visiting_home_guard(layout):
        pass
    selector = parse_address(layout.address)
    assert selector is not None
    assert resolve_address(root, selector)[0] == layout
    assert not (root / ".runtime").exists()
    assert (
        layout.place
        == isolated_visiting_base / root_code(str(root)) / layout.place_code
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
    "text",
    [
        "aid@r0000000000000000",
        "aid@s000",
        "aid@h000",
        "aid@S00000000000000000",
        "aid@s" + "0" * 17,
        "lex@t" + "0" * 10,
        "aid@8" + "0" * 18,
        "lex@q" + "0" * 9,
    ],
)
def test_partial_and_previous_ids_are_rejected(text):
    with pytest.raises(ValueError):
        parse_address(text)


def test_previous_complete_visiting_marker_is_rejected():
    with pytest.raises(ValueError, match="place code"):
        parse_address("lex@h0000000000")


def test_source_path_cannot_select_a_visiting_code(tmp_path):
    from toolang.common.place_codes import visiting_place_code

    code = visiting_place_code("https://example.com/lex.too")
    with pytest.raises(ValueError, match="roaming code"):
        parse_address(f"{tmp_path / 'lex.too'}@{code}")


def test_previous_binding_fields_are_not_accepted(tmp_path):
    from toolang.up.identity import identity_bindings, layout_from_control

    layout = AgentLayout.resident(tmp_path, "aid")
    control = identity_bindings(layout)
    assert control["root"] == control["place"] == str(tmp_path)
    control["toolang_root"] = control.pop("root")
    with pytest.raises(ValueError, match="canonical absolute path"):
        layout_from_control(control)


def test_empty_unpublished_candidate_is_skipped_without_overwriting_it(
    tmp_path, monkeypatch
):
    from toolang.common.place_codes import roaming_place_code

    monkeypatch.setattr(
        "toolang.up.allocation.time.time_ns",
        lambda: (PLACE_CODE_EPOCH_MS + 100) * 1_000_000,
    )
    source = tmp_path / "aid.too"
    root = tmp_path / "root"
    first = allocate_roaming(source, toolang_root=root)
    occupied = first.place.parent / roaming_place_code(
        str(source), PLACE_CODE_EPOCH_MS + 101
    )
    occupied.mkdir(mode=0o700)
    second = allocate_roaming(source, toolang_root=root)
    assert second.request_order_ns == (PLACE_CODE_EPOCH_MS + 102) * 1_000_000
    assert occupied.is_dir() and not tuple(occupied.iterdir())
    assert select_roaming(source, toolang_root=root).place == second.place


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


def test_missing_root_ownership_is_not_recreated_over_retained_data(tmp_path):
    root = tmp_path / "root"
    source = tmp_path / "aid.too"
    layout = allocate_roaming(source, toolang_root=root)
    ownership = layout.place.parent / "root.json"
    ownership.unlink()
    before = layout.runtime_status.read_bytes()
    with pytest.raises(ValueError, match="unbound root namespace"):
        allocate_roaming(source, toolang_root=root)
    assert not ownership.exists()
    assert layout.runtime_status.read_bytes() == before


def test_path_selection_revalidates_the_ownership_snapshot_it_pins(
    tmp_path, monkeypatch
):
    from toolang.up import addresses
    from toolang.up.instance import update_instance

    root = tmp_path / "root"
    source = tmp_path / "aid.too"
    layout = allocate_roaming(source, toolang_root=root)
    select = addresses.select_roaming

    def replaced(*args, **kwargs):
        selected = select(*args, **kwargs)

        def replace(entry):
            entry["launch_id"] = "replacement"
            entry["control"]["source_ref"] = "https://other.example/aid.too"

        update_instance(layout.runtime_status, replace)
        return selected

    monkeypatch.setattr(addresses, "select_roaming", replaced)
    selector = parse_address(f"{source}@{layout.place_code}")
    assert selector is not None
    with pytest.raises(ValueError, match="identity"):
        resolve_address(root, selector)
