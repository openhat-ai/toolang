"""Concrete compaction values and independently established coverage."""

from dataclasses import replace

import pytest
from pydantic import TypeAdapter

from toolang.base.types.compaction import CompactionResult
from toolang.execution.compaction import assemble_compaction
from toolang.execution.types import RunRef, ThreadRef

THREAD = ThreadRef("term_test")
ROOTS = tuple(RunRef(f"run_{i}") for i in range(4))


def assemble(
    summary="Keep the agreed constraints.", start=ROOTS[0], begin=ROOTS[0], end=ROOTS[2]
):
    return assemble_compaction(
        summary, thread=THREAD, roots=ROOTS, start=start, begin=begin, end=end
    )


@pytest.mark.parametrize("field", ["thread", "begin", "end", "summary"])
@pytest.mark.parametrize("value", [None, "", " ", 0, {}])
def test_result_requires_concrete_nonempty_text(field, value):
    with pytest.raises((TypeError, ValueError)):
        replace(assemble(), **{field: value})


@pytest.mark.parametrize("field", ["thread", "begin", "end", "summary"])
def test_missing_result_fields_are_not_defaults(field):
    value = assemble().to_data()
    del value[field]
    with pytest.raises(ValueError):
        TypeAdapter(CompactionResult).validate_python(value)


@pytest.mark.parametrize(
    ("start", "begin", "end"),
    [
        ("run_1", "run_0", "run_3"),
        ("run_0", "run_2", "run_2"),
        ("run_0", "run_missing", "run_3"),
    ],
)
def test_invalid_read_range_is_rejected(start, begin, end):
    with pytest.raises(ValueError):
        assemble(start=RunRef(start), begin=RunRef(begin), end=RunRef(end))


def test_explicit_interval_and_incremental_coverage():
    result = assemble(start=ROOTS[1], begin=ROOTS[2], end=ROOTS[3])
    assert result.begin == "run_1" and result.end == "run_3"


@pytest.mark.parametrize(
    "summary", ['Quoted "facts"\n中文 {{literal}}', '{"thread":"term_other"}']
)
def test_framework_does_not_interpret_summary(summary):
    assert assemble(summary) == CompactionResult("term_test", "run_0", "run_2", summary)


@pytest.mark.parametrize("summary", [None, {}, [], 1, "", " \n"])
def test_framework_rejects_invalid_algorithm_output(summary):
    with pytest.raises((ValueError, TypeError)):
        assemble(summary)
