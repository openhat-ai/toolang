"""Removed Flow syntax and storage layouts have no compatibility reader."""

from typing import cast

import pytest

from toolang.execution.records import step_given_from_data
from toolang.lang.ast import RunStmt, Span, to_data


@pytest.mark.parametrize(
    "kind,step_kind",
    [("scatter", "run"), ("gather", "run"), ("storm", "par"), ("settle", "loop")],
)
def test_removed_statement_records_are_rejected(kind, step_kind) -> None:
    data = cast(
        dict[str, object], to_data(RunStmt(span=Span(line=1), runnable="worker"))
    )
    data["kind"] = kind
    with pytest.raises(ValueError, match="Removed"):
        step_given_from_data(step_kind, data)
