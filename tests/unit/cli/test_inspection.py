from __future__ import annotations

import pytest

from toolang.cli.common.inspection import (
    HistoricalModelCallOwner,
    ModelCallInspectTarget,
    ProspectiveModelCallOwner,
    RunInspectTarget,
    StepInspectTarget,
    ThreadInspectTarget,
    inspect_target_requires_program,
    parse_inspect_target,
)
from toolang.execution.types import StepPath


def test_parse_inspect_target_returns_distinct_typed_variants() -> None:
    assert parse_inspect_target("term_one") == ThreadInspectTarget("term_one")
    assert parse_inspect_target("run_one") == RunInspectTarget("run_one")
    assert parse_inspect_target("run_one.0.2") == StepInspectTarget(
        StepPath("run_one", (0, 2))
    )
    assert parse_inspect_target("model_call@run_one.0.2") == ModelCallInspectTarget(
        HistoricalModelCallOwner(StepPath("run_one", (0, 2)))
    )
    assert parse_inspect_target("model_call@agic:review") == ModelCallInspectTarget(
        ProspectiveModelCallOwner("review")
    )


def test_only_prospective_model_call_requires_program_materialization() -> None:
    historical = parse_inspect_target("model_call@run_one.0")
    prospective = parse_inspect_target("model_call@agic:review")

    assert inspect_target_requires_program(historical) is False
    assert inspect_target_requires_program(prospective) is True


@pytest.mark.parametrize(
    "value",
    (
        "model_call@",
        "model_call@run_one",
        "model_call@agic:",
        "model_call@agic:review.extra",
        "model_call@run_one.01",
    ),
)
def test_parse_inspect_target_rejects_invalid_model_call_owners(value: str) -> None:
    with pytest.raises(ValueError):
        parse_inspect_target(value)
