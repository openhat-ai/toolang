"""Shared run activity projections for CLI and TUI surfaces."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from .labels import executable_label


@dataclass(slots=True)
class FlowStageView:
    """One projected flow stage."""

    key: str
    index: int | None = None
    total: int | None = None
    kind: str = "stage"
    title: str = "stage"
    status: str = "running"
    input_shape: str | None = None
    output_shape: str | None = None
    item_total: int | None = None
    parallelism: int | None = None
    calls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FlowCallView:
    """One projected child call inside a flow stage."""

    key: str
    stage_key: str
    label: str
    run_id: str
    status: str = "running"
    item_index: int | None = None
    item_count: int | None = None
    lane_index: int | None = None
    parallelism: int | None = None
    run: Mapping[str, Any] | None = None


def project_flow_from_run(
    run: Mapping[str, Any],
    *,
    run_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[FlowStageView], dict[str, FlowCallView]]:
    """Project durable run detail into flow stages and calls."""

    return _project_flow_steps(
        (_mapping(step.get("record")) for step in _run_steps(run)),
        run_by_id=run_by_id or {},
    )


def project_flow_from_step_payloads(
    steps: Sequence[Mapping[str, Any]],
    *,
    run_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[FlowStageView], dict[str, FlowCallView]]:
    """Project live step payloads into flow stages and calls."""

    return _project_flow_steps(steps, run_by_id=run_by_id or {})


def stage_calls(stage: FlowStageView, calls: Mapping[str, FlowCallView]) -> list[FlowCallView]:
    """Return projected calls for one stage in stable display order."""

    items = [calls[key] for key in stage.calls if key in calls]
    return sorted(
        items,
        key=lambda call: (
            call.lane_index if call.lane_index is not None else 999_999,
            call.item_index if call.item_index is not None else 999_999,
            call.run_id,
        ),
    )


def stage_lanes(calls: Sequence[FlowCallView]) -> dict[int, list[FlowCallView]]:
    """Group projected calls by lane index."""

    lanes: dict[int, list[FlowCallView]] = {}
    for call in calls:
        lanes.setdefault(call.lane_index if call.lane_index is not None else 0, []).append(call)
    return lanes


def stage_prefix(stage: FlowStageView) -> str:
    """Return the compact status marker for one stage."""

    if stage.status in {"succeeded", "done"}:
        return "✓"
    if stage.status == "failed":
        return "✗"
    return "…"


def stage_label(stage: FlowStageView, *, width: int = 56) -> str:
    """Return the compact inspect label for one stage."""

    return _truncate(stage_title_label(stage), width=width)


def stage_title_label(stage: FlowStageView) -> str:
    """Return the stage title with a bracketed index."""

    index = "?"
    if stage.index is not None:
        index = str(stage.index + 1)
    title = " ".join(stage.title.split()) or stage.kind
    return f"[{index}] {title}"


def stage_tail(stage: FlowStageView, calls: Mapping[str, FlowCallView]) -> str:
    """Return compact status details for one projected stage."""

    lanes = f"{stage.parallelism} lanes" if stage.parallelism and stage.parallelism > 1 else ""
    if stage.status in {"succeeded", "done"} and (stage.input_shape or stage.output_shape):
        shape = f"{stage.input_shape or '?'} -> {stage.output_shape or '?'}"
        return " · ".join(item for item in (shape, lanes) if item)
    stage_call_items = stage_calls(stage, calls)
    done = sum(1 for call in stage_call_items if call.status in {"succeeded", "done", "failed", "canceled"})
    failed = sum(1 for call in stage_call_items if call.status == "failed")
    if stage.item_total is not None:
        progress = f"{done}/{stage.item_total} items"
        if failed:
            progress = f"{progress} · {failed} failed"
        return " · ".join(item for item in (progress, lanes) if item)
    if failed:
        return f"{failed} failed"
    return "running"


def shape_label(preview: object, *, fallback_count: int | None = None) -> str:
    """Return a compact shape label for a serialized value preview."""

    if isinstance(preview, Mapping):
        preview = cast(Mapping[str, Any], preview)
        count = _int_or_none(preview.get("count"))
        if count is not None:
            return _items_label(count)
        if preview.get("type") == "list":
            count = _int_or_none(preview.get("count"))
            if count is not None:
                return _items_label(count)
        if preview.get("type") == "object":
            if fallback_count is not None:
                return _items_label(fallback_count)
            return "object"
    if isinstance(preview, str):
        lines = [line.strip() for line in preview.splitlines() if line.strip()]
        if 1 < len(lines) <= 20:
            return f"{len(lines)} items"
    if preview is None:
        return "unset"
    return "1 item"


def preview_count(preview: object) -> int | None:
    """Return the serialized preview count when present."""

    if isinstance(preview, Mapping):
        return _int_or_none(cast(Mapping[str, Any], preview).get("count"))
    return None


def output_count(record: Mapping[str, Any]) -> int | None:
    """Return a count parsed from text output when available."""

    for part in _list(record.get("output")):
        typed = _mapping(part)
        if typed.get("type") != "text":
            continue
        text = _text(typed.get("text"))
        if not text:
            continue
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            continue
        if isinstance(parsed, Mapping):
            return _int_or_none(parsed.get("count"))
    return None


def flow_stage_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return merged stage metadata from a flow step payload."""

    return _stage_context(payload)


def child_run_ids(payload: Mapping[str, Any], record: Mapping[str, Any]) -> tuple[str, ...]:
    """Return child run ids referenced by one child-call payload."""

    return _child_run_ids(payload, record)


def _project_flow_steps(
    steps: Sequence[Mapping[str, Any]] | Any,
    *,
    run_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[list[FlowStageView], dict[str, FlowCallView]]:
    stage_order: list[str] = []
    stages: dict[str, FlowStageView] = {}
    calls: dict[str, FlowCallView] = {}
    for step in steps:
        record = _mapping(step)
        payload = _mapping(record.get("payload"))
        if not payload and isinstance(record.get("metadata"), Mapping):
            payload = _mapping(record.get("metadata"))
        kind = _text(record.get("kind"))
        if kind in {"step", "parallel", "bind"}:
            stage = _ensure_stage(payload, stages=stages, stage_order=stage_order)
            metadata = _mapping(payload.get("metadata"))
            op = _text(payload.get("op")) or _text(metadata.get("op")) or ""
            input_preview = metadata.get("input_preview")
            if input_preview is not None:
                stage.input_shape = shape_label(input_preview)
            output_preview = payload.get("output_preview")
            if output_preview is not None:
                if op.startswith("prepare_"):
                    stage.item_total = preview_count(output_preview) or stage.item_total
                    stage.status = "running"
                if op == "set_current":
                    stage.output_shape = shape_label(
                        output_preview,
                        fallback_count=output_count(record),
                    )
                    stage.status = "succeeded"
            continue
        if kind != "run":
            continue
        stage = _ensure_stage(payload, stages=stages, stage_order=stage_order)
        for run_id in _child_run_ids(payload, record):
            child_run = run_by_id.get(run_id)
            child_output = _mapping(child_run.get("output")) if child_run is not None else {}
            call = _ensure_call(
                payload,
                run_id=run_id,
                stage=stage,
                calls=calls,
                child_run=child_run,
                fallback_status=_display_status(record.get("status")),
            )
            if child_output:
                call.status = _display_status(child_output.get("status"))
            else:
                call.status = _display_status(record.get("status"))
            if call.status == "failed":
                stage.status = "failed"
    ordered = [stages[key] for key in stage_order]
    for stage in ordered:
        stage_call_items = stage_calls(stage, calls)
        if any(call.status == "failed" for call in stage_call_items):
            stage.status = "failed"
        elif (
            stage.status == "running"
            and stage_call_items
            and all(call.status in {"succeeded", "done"} for call in stage_call_items)
            and (stage.item_total is None or len(stage_call_items) >= stage.item_total)
        ):
            stage.status = "succeeded"
    return ordered, calls


def _ensure_stage(
    payload: Mapping[str, Any],
    *,
    stages: dict[str, FlowStageView],
    stage_order: list[str],
) -> FlowStageView:
    ctx = _stage_context(payload)
    stage_index = _int_or_none(ctx.get("stage_index"))
    key = f"stage:{stage_index}" if stage_index is not None else "stage"
    stage = stages.get(key)
    if stage is None:
        stage = FlowStageView(key=key)
        stages[key] = stage
        stage_order.append(key)
    stage.index = stage_index if stage_index is not None else stage.index
    stage.total = _int_or_none(ctx.get("stage_total")) or stage.total
    stage.kind = _text(ctx.get("stage_kind")) or stage.kind
    stage.title = (
        _text(ctx.get("stage_title"))
        or _text(ctx.get("stage_doc"))
        or _text(ctx.get("stage_target"))
        or _clean_stage_title(_text(ctx.get("stage_label")))
        or stage.title
    )
    stage.parallelism = _int_or_none(ctx.get("parallelism")) or stage.parallelism
    if item_count := _int_or_none(ctx.get("item_count")):
        stage.item_total = item_count
    if input_preview := ctx.get("input_preview"):
        stage.input_shape = shape_label(input_preview)
    return stage


def _ensure_call(
    payload: Mapping[str, Any],
    *,
    run_id: str,
    stage: FlowStageView,
    calls: dict[str, FlowCallView],
    child_run: Mapping[str, Any] | None,
    fallback_status: str,
) -> FlowCallView:
    call = calls.get(run_id)
    ctx = _stage_context(payload)
    if call is None:
        target = executable_label(
            _text(payload.get("target_kind")) or "run",
            _text(payload.get("target")),
            metadata=_mapping(payload.get("metadata")),
        ).replace(":", " ", 1)
        call = FlowCallView(
            key=run_id,
            stage_key=stage.key,
            label=_call_label(target, ctx),
            run_id=run_id,
            status=fallback_status,
            run=child_run,
        )
        calls[run_id] = call
        stage.calls.append(run_id)
    call.item_index = _int_or_none(ctx.get("item_index")) if ctx.get("item_index") is not None else call.item_index
    call.item_count = _int_or_none(ctx.get("item_count")) or call.item_count
    call.lane_index = _int_or_none(ctx.get("lane_index")) if ctx.get("lane_index") is not None else call.lane_index
    call.parallelism = _int_or_none(ctx.get("parallelism")) or call.parallelism
    if call.parallelism is not None:
        stage.parallelism = call.parallelism
    if call.item_count is not None:
        stage.item_total = call.item_count
    return call


def _stage_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _mapping(payload.get("metadata"))
    child = _mapping(payload.get("child")) or _mapping(metadata.get("child"))
    ctx: dict[str, Any] = dict(child)
    ctx.update(metadata)
    ctx.update(payload)
    if "item_index" not in ctx:
        item_indexes = _list(payload.get("item_indexes"))
        if item_indexes:
            ctx["item_index"] = item_indexes[0]
    return ctx


def _child_run_ids(payload: Mapping[str, Any], record: Mapping[str, Any]) -> tuple[str, ...]:
    child_ids = tuple(str(item) for item in _list(payload.get("child_run_ids")) if item is not None)
    if child_ids:
        return child_ids
    return (f"{record.get('step_index', 'step')}",)


def _call_label(target: str, ctx: Mapping[str, Any]) -> str:
    item_index = _int_or_none(ctx.get("item_index"))
    item_count = _int_or_none(ctx.get("item_count"))
    if item_index is None:
        return target
    item = f"item {item_index + 1}/{item_count}" if item_count else f"item {item_index + 1}"
    return f"{item} · {target}"


def _run_steps(run: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    output = _mapping(run.get("output"))
    return [_mapping(item) for item in _list(output.get("steps"))]


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else []


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return None


def _display_status(value: object) -> str:
    text = _text(value)
    if text == "finished":
        return "succeeded"
    return text or "unknown"


def _items_label(count: int) -> str:
    return "1 item" if count == 1 else f"{count} items"


def _clean_stage_title(label: str | None) -> str | None:
    if not label:
        return None
    if ": " in label:
        return label.split(": ", 1)[1]
    return label


def _truncate(text: str, *, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return f"{text[: width - 1]}…"
