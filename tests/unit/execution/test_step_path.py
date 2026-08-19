from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from toolang.execution.types import StepPath


def test_step_path_round_trips_global_and_local_forms() -> None:
    path = StepPath.parse("run_root.2.3")

    assert path.run == "run_root"
    assert path.indices == (2, 3)
    assert path.local == "2.3"
    assert path.index == 3
    assert path.parent == StepPath.parse("run_root.2")
    assert path.child(4) == StepPath.parse("run_root.2.3.4")
    assert StepPath.from_local("run_root", "2.3") == path
    assert TypeAdapter(StepPath).dump_json(path) == b'"run_root.2.3"'


@pytest.mark.parametrize(
    "value",
    (
        "",
        "run_root",
        "run_root.",
        ".0",
        "run_root.-1",
        "run_root.01",
        "run_root/0",
        "run_root.0/1",
    ),
)
def test_step_path_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError):
        StepPath.parse(value)
