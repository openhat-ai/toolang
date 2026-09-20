"""Decode producer output using explicit, independently established coverage."""

from collections.abc import Mapping, Sequence
from typing import cast

from .types import CompactionResult, RunRef, ThreadRef


def decode_compaction(
    raw: object,
    *,
    thread: ThreadRef,
    roots: Sequence[RunRef],
    request: Mapping[str, object],
    previous: CompactionResult | None = None,
) -> CompactionResult:
    """Normalize legacy null only when recorded coverage proves a full prefix."""
    if not isinstance(raw, Mapping):
        raise ValueError("compact output must be an object")
    value = cast(Mapping[str, object], raw)
    if not {"thread", "begin", "end", "summary"} <= value.keys() or not roots:
        raise ValueError("compact output requires thread, begin, end, and summary")
    if request.get("thread") != str(thread) or value["thread"] != str(thread):
        raise ValueError("compact output targets another Thread")
    begin = request.get("begin", str(roots[0]))
    # Old runtime controls explicitly stored null for an omitted begin.
    if begin is None:
        begin = str(roots[0])
    if not isinstance(begin, str) or not isinstance(request.get("end"), str):
        raise ValueError("compact request requires concrete coverage")
    expected_begin = RunRef.parse(begin)
    if request.get("previous") is not None:
        if previous is None or request.get("bare") is True:
            raise ValueError("compact previous must identify a validated summary")
        previous.validate_coverage(thread, roots)
        if previous.begin != roots[0] or previous.end != expected_begin:
            raise ValueError("compact previous coverage must be a contiguous prefix")
        expected_begin = previous.begin
    output_begin = value["begin"]
    if output_begin is None and expected_begin == roots[0]:
        output_begin = str(roots[0])
    if not isinstance(output_begin, str) or not isinstance(value["end"], str):
        raise ValueError("compact bounds must be concrete Run references")
    if output_begin != str(expected_begin) or value["end"] != request["end"]:
        raise ValueError("compact output must match its requested coverage")
    if not isinstance(value["summary"], str):
        raise ValueError("compact summary must be text")
    result = CompactionResult(
        thread, expected_begin, RunRef.parse(value["end"]), value["summary"]
    )
    result.validate_coverage(thread, roots)
    return result
