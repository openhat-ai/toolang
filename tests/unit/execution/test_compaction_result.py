"""Framework compaction coverage does not depend on authored type metadata."""

from dataclasses import replace

import pytest

from toolang.execution.compaction import decode_compaction
from toolang.execution.types import CompactionResult, RunRef, ThreadRef


THREAD = ThreadRef("term_test")
ROOTS = tuple(RunRef(f"run_{i}") for i in range(4))
REQUEST = {"thread": str(THREAD), "begin": "run_0", "end": "run_2"}
OUTPUT = {**REQUEST, "summary": "Keep the agreed constraints."}


def decode(output=OUTPUT, request=REQUEST, previous=None):
    return decode_compaction(
        output, thread=THREAD, roots=ROOTS, request=request, previous=previous
    )


@pytest.mark.parametrize("begin", [None, "run_0"])
def test_full_prefix_normalizes_to_concrete_fields(begin):
    result = decode({**OUTPUT, "begin": begin})
    assert result == CompactionResult(THREAD, ROOTS[0], ROOTS[2], OUTPUT["summary"])
    assert result.to_data() == OUTPUT


@pytest.mark.parametrize("field", ["thread", "begin", "end", "summary"])
def test_missing_result_fields_are_not_defaults(field):
    output = dict(OUTPUT)
    del output[field]
    with pytest.raises(ValueError):
        decode(output)


@pytest.mark.parametrize(
    "patch",
    [
        {"thread": "term_other"},
        {"thread": None},
        {"begin": 0},
        {"begin": "run_missing"},
        {"begin": "run_1"},
        {"end": None},
        {"end": "run_0"},
        {"end": "run_missing"},
        {"summary": None},
        {"summary": " \n"},
        {"summary": {}},
    ],
)
def test_invalid_result_is_rejected(patch):
    with pytest.raises(ValueError):
        decode({**OUTPUT, **patch})


def test_interval_cannot_claim_a_full_prefix():
    request = {**REQUEST, "begin": "run_1"}
    result = decode({**OUTPUT, "begin": "run_1"}, request)
    assert result.begin == ROOTS[1]
    with pytest.raises(ValueError):
        decode({**OUTPUT, "begin": None}, request)


def test_previous_requires_validated_contiguous_full_prefix():
    previous = decode()
    request = {
        **REQUEST,
        "begin": "run_2",
        "end": "run_3",
        "previous": "run_old/output",
    }
    output = {**OUTPUT, "begin": None, "end": "run_3"}
    result = decode(output, request, previous)
    assert result.begin == ROOTS[0] and result.end == ROOTS[3]
    for invalid in (
        None,
        replace(previous, thread=ThreadRef("term_other")),
        replace(previous, end=ROOTS[1]),
        replace(previous, begin=ROOTS[1]),
    ):
        with pytest.raises(ValueError):
            decode(output, request, invalid)
    with pytest.raises(ValueError):
        decode(output, {**request, "bare": True}, previous)


@pytest.mark.parametrize("field", ["thread", "begin", "end", "summary"])
def test_concrete_type_rejects_none(field):
    with pytest.raises((TypeError, ValueError)):
        replace(decode(), **{field: None})
