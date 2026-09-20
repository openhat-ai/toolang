"""Concrete compaction values and independently established coverage."""

from dataclasses import replace

import pytest
from pydantic import TypeAdapter

from toolang.base.types.compaction import CompactionResult
from toolang.execution.compaction import assemble_compaction, algorithm_input
from toolang.execution.types import RunRef, ThreadRef
from toolang.lang.input import RunnableInput

THREAD = ThreadRef("term_test")
ROOTS = tuple(RunRef(f"run_{i}") for i in range(4))


def assemble(
    summary="Keep the agreed constraints.", begin=ROOTS[0], end=ROOTS[2], previous=None
):
    return assemble_compaction(
        summary, thread=THREAD, roots=ROOTS, begin=begin, end=end, previous=previous
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


def test_previous_requires_validated_contiguous_full_prefix():
    previous = assemble()
    assert assemble(begin=ROOTS[2], end=ROOTS[3], previous=previous).begin == "run_0"
    for invalid in (
        replace(previous, thread="term_other"),
        replace(previous, end="run_1"),
        replace(previous, begin="run_1"),
    ):
        with pytest.raises(ValueError):
            assemble(begin=ROOTS[2], end=ROOTS[3], previous=invalid)


@pytest.mark.parametrize("end", ["run_1", "run_2", "run_missing"])
def test_incremental_coverage_must_advance_before_merging(end):
    with pytest.raises(ValueError):
        assemble(begin=ROOTS[2], end=RunRef(end), previous=assemble())


@pytest.mark.parametrize(
    "summary", ['Quoted "facts"\n中文 {{literal}}', '{"thread":"term_other"}']
)
def test_framework_does_not_interpret_summary(summary):
    assert assemble(summary) == CompactionResult("term_test", "run_0", "run_2", summary)


@pytest.mark.parametrize("summary", [None, {}, [], 1, "", " \n"])
def test_framework_rejects_invalid_algorithm_output(summary):
    with pytest.raises((ValueError, TypeError)):
        assemble(summary)


def test_incremental_algorithm_gets_text_but_result_retains_complete_coverage():
    previous = assemble()
    request = RunnableInput({"thread": "term_test", "begin": "run_2", "end": "run_3"})
    assert dict(algorithm_input(request, previous)) == {
        **request,
        "previous_summary": previous.summary,
    }
    result = assemble("Combined.", begin=ROOTS[2], end=ROOTS[3], previous=previous)
    assert result.begin == "run_0" and result.end == "run_3"
