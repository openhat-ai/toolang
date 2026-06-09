"""Human-readable execution labels for CLI progress and inspection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast


def executable_label(kind: str | None, name: str | None, *, metadata: Mapping[str, Any] | None = None) -> str:
    """Return a compact label for one thunk, flow, or run-like executable."""

    normalized_kind = (kind or "run").strip() or "run"
    normalized_name = (name or "").strip()
    if normalized_name:
        return f"{normalized_kind}:{normalized_name}"
    line = _source_line(metadata)
    if normalized_kind in {"thunk", "flow"} and line is not None:
        return f"{normalized_kind}:<L{line}>"
    if normalized_kind in {"thunk", "flow"}:
        return f"inline {normalized_kind}"
    return normalized_kind


def flow_op_summary(payload: Mapping[str, Any]) -> str:
    """Return one summary for a deterministic flow operation step."""

    label = stage_label(payload)
    phase = _flow_op_phase(_text(payload.get("op")))
    count = _preview_count(payload.get("output_preview"))
    return " ".join(item for item in (label, phase, count) if item)


def child_call_summary(payload: Mapping[str, Any]) -> str:
    """Return one summary for a child-call step."""

    leaf = child_leaf_summary(payload)
    stage = stage_label(payload)
    if stage:
        return " ".join(item for item in (stage, "->", leaf) if item)
    return leaf


def child_leaf_summary(payload: Mapping[str, Any]) -> str:
    """Return a child-call summary relative to its parent stage."""

    target = executable_label(
        _text(payload.get("target_kind")) or "run",
        _text(payload.get("target")),
        metadata=_mapping(payload.get("metadata")),
    )
    children = ", ".join(item for item in (_text(raw) for raw in _sequence(payload.get("child_run_ids"))) if item)
    lane = lane_label(payload)
    return " ".join(item for item in (lane, target, children) if item)


def stage_label(payload: Mapping[str, Any]) -> str:
    """Return one stage label from a step payload or child metadata."""

    metadata = _mapping(payload.get("metadata"))
    child = _child_metadata(payload, metadata)
    stage_index = _int_or_none(payload.get("stage_index"))
    if stage_index is None:
        stage_index = _int_or_none(metadata.get("stage_index"))
    if stage_index is None:
        stage_index = _int_or_none(child.get("stage_index"))
    stage_kind = _text(payload.get("stage_kind")) or _text(metadata.get("stage_kind"))
    if stage_kind is None:
        stage_kind = _text(child.get("stage_kind"))
    label = _text(payload.get("stage_label")) or _text(metadata.get("stage_label"))
    if label is None:
        label = _text(child.get("stage_label"))
    if label is None and stage_kind is not None:
        label = stage_kind
    if label is None:
        return ""
    if stage_index is None:
        return f"stage {label}"
    return f"stage {stage_index + 1} {label}"


def lane_label(payload: Mapping[str, Any]) -> str:
    """Return a compact lane/item label when a stage child was parallelized."""

    metadata = _mapping(payload.get("metadata"))
    child = _child_metadata(payload, metadata)
    lane_index = _int_or_none(payload.get("lane_index"))
    if lane_index is None:
        lane_index = _int_or_none(metadata.get("lane_index"))
    if lane_index is None:
        lane_index = _int_or_none(child.get("lane_index"))
    parallelism = _int_or_none(payload.get("parallelism"))
    if parallelism is None:
        parallelism = _int_or_none(metadata.get("parallelism"))
    if parallelism is None:
        parallelism = _int_or_none(child.get("parallelism"))
    item_index = _first_item_index(payload)
    if item_index is None:
        item_index = _int_or_none(metadata.get("item_index"))
    if item_index is None:
        item_index = _int_or_none(child.get("item_index"))
    item_count = _int_or_none(payload.get("item_count"))
    if item_count is None:
        item_count = _int_or_none(metadata.get("item_count"))
    if item_count is None:
        item_count = _int_or_none(child.get("item_count"))
    pieces: list[str] = []
    if lane_index is not None and parallelism and parallelism > 1:
        pieces.append(f"lane {lane_index + 1}/{parallelism}")
    if item_index is not None:
        if item_count:
            pieces.append(f"item {item_index + 1}/{item_count}")
        else:
            pieces.append(f"item {item_index + 1}")
    return " ".join(pieces)


def _flow_op_phase(op: str | None) -> str:
    if op is None:
        return ""
    if op.startswith("prepare_"):
        return "prepare"
    if op == "set_current":
        return "done"
    return op


def _preview_count(value: Any) -> str:
    if isinstance(value, Mapping):
        count = value.get("count")
        if isinstance(count, int):
            return f"count={count}"
    return ""


def _first_item_index(payload: Mapping[str, Any]) -> int | None:
    items = _sequence(payload.get("item_indexes"))
    for item in items:
        value = _int_or_none(item)
        if value is not None:
            return value
    return None


def _source_line(metadata: Mapping[str, Any] | None) -> int | None:
    if metadata is None:
        return None
    line = _int_or_none(metadata.get("source_line"))
    if line is not None:
        return line
    child = _mapping(metadata.get("child"))
    return _int_or_none(child.get("source_line"))


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _child_metadata(payload: Mapping[str, Any], metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(payload.get("child")) or _mapping(metadata.get("child"))


def _sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, str) else ()


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
