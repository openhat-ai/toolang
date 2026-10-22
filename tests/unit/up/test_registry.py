"""Discovery references retain their format and strict path/launch boundary."""

import json

import pytest

from toolang.up.registry import RuntimeReference, read_reference, registration_current


def test_reference_round_trip_and_both_ownership_fences(tmp_path):
    reference = RuntimeReference(tmp_path / "status.json", "launch")
    payload = {
        "version": 5,
        "status": str(tmp_path / "status.json"),
        "launch_id": "launch",
    }
    assert reference.to_data() == payload
    assert RuntimeReference.from_data(payload) == reference
    path = tmp_path / "alice.json"
    path.write_text(json.dumps(payload))
    assert read_reference(path) == reference
    assert registration_current(path, "launch", tmp_path / "status.json")
    assert not registration_current(path, "replacement", tmp_path / "status.json")
    assert not registration_current(path, "launch", tmp_path / "other.json")


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
