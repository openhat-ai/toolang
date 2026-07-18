"""Invoke results and trace progress rendering."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import cast

from rich.console import Console
from rich.live import Live
from rich.text import Text
import typer

from toolang.agent import local as agents
from toolang.config.log_spec import PY_LOG_ENV_VAR
from toolang.execution.events import RunEnd, RunStarting, StepBegin, StepEnd, TraceEvent
from toolang.execution.records import trace_index, trace_run
from toolang.execution.records import RunRecord
from toolang.execution.store import RunStore, run_store_path
from toolang.plugin.models.errors import NO_AVAILABLE_MODELS_MESSAGE, NO_MATCHED_MODELS_MESSAGE
from toolang.cli.common.output import executable_label


def emit_outcome(
    outcome: RunRecord,
    *,
    toolang_root: Path,
    agent_name: str,
    executable_name: str | None,
) -> int:
    if outcome.status != "finished":
        error = outcome.error or "invoke failed"
        typer.echo(f"toolang error: {error}", err=True)
        if _is_model_selection_error(error):
            return 1
        typer.echo(f"Run: {outcome.run_id}", err=True)
        log_path = agents.agent_script_run_log_path(
            toolang_root,
            agent_name,
            executable_name=executable_name,
            run_id=outcome.run_id,
        )
        if log_path.exists():
            typer.echo(f"Log: {log_path}", err=True)
        return 1
    store = RunStore(run_store_path(toolang_root, agent_name))
    try:
        output = store.run_output_text(run_id=outcome.run_id)
    finally:
        store.close()
    if output:
        typer.echo(output)
    return 0


def _is_model_selection_error(error: str) -> bool:
    return error in {NO_AVAILABLE_MODELS_MESSAGE, NO_MATCHED_MODELS_MESSAGE}


def progress_sink(
    *, executable_name: str | None, quiet: bool, verbosity: int
) -> "ScriptProgressSink":
    return ScriptProgressSink(
        executable_name=executable_name or "default",
        render=not quiet and sys.stderr.isatty(),
        verbosity=verbosity,
    )


def emit_interrupt(
    *,
    script_progress: "ScriptProgressSink | None",
    toolang_root: Path | None,
    agent_name: str | None,
    executable_name: str | None,
    environ: dict[str, str] | None,
) -> None:
    typer.echo("toolang interrupted", err=True)
    run_id = script_progress.run_id if script_progress is not None else None
    if run_id:
        typer.echo(f"Run: {run_id}", err=True)
    if not run_id or toolang_root is None or agent_name is None:
        return
    if environ is None or not environ.get(PY_LOG_ENV_VAR, "").strip():
        return
    typer.echo(
        f"Log: {agents.agent_script_run_log_path(toolang_root, agent_name, executable_name=executable_name, run_id=run_id)}",
        err=True,
    )


@dataclass(slots=True)
class _StageProgress:
    key: str
    index: int | None = None
    kind: str = "stage"
    title: str = "stage"
    status: str = "running"
    input_shape: str | None = None
    output_shape: str | None = None
    item_total: int | None = None
    parallelism: int | None = None
    calls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _CallProgress:
    key: str
    stage_key: str
    label: str
    run_id: str
    status: str = "running"
    item_index: int | None = None
    item_count: int | None = None
    lane_index: int | None = None
    parallelism: int | None = None
    steps: dict[int, str] = field(default_factory=dict)


class ScriptProgressSink:
    """Render script progress to stderr without touching stdout."""

    wants_stream = False

    def __init__(self, *, executable_name: str, render: bool, verbosity: int = 0) -> None:
        self._executable_name = executable_name
        self._render_enabled = render
        self._verbosity = max(0, verbosity)
        self._run_id: str | None = None
        self._title = ""
        self._finished = False
        self._stage_order: list[str] = []
        self._stages: dict[str, _StageProgress] = {}
        self._calls: dict[str, _CallProgress] = {}
        self._run_call_keys: dict[str, str] = {}
        self._console = Console(file=sys.stderr, force_terminal=True, highlight=False)
        self._live: Live | None = None

    @property
    def run_id(self) -> str | None:
        return self._run_id

    def on_event(self, event: TraceEvent) -> None:
        if event.type == "run_starting":
            event = cast(RunStarting, event)
            executable = self._mapping(event.context.get("executable"))
            label = executable_label(
                self._text(executable.get("kind")) or "run",
                self._text(executable.get("name")),
            )
            if event.parent is None:
                self._run_id = event.run
                self._title = f"Running {label}: {event.run}"
                self._render()
                return
            if event.parent is not None and trace_run(event.parent) == self._run_id:
                stage = self._ensure_stage(
                    {**dict(event.context), "_step": event.parent}
                )
                call = self._ensure_call(
                    run_id=event.run,
                    stage=stage,
                    target_label=label,
                    payload=event.context,
                )
                call.status = "running"
                self._render()
            return
        if event.type == "step_begin":
            event = cast(StepBegin, event)
            if trace_run(event.step) == self._run_id and event.context.get("statement"):
                self._ensure_stage({**event.context, "_step": event.step})
                self._render()
            else:
                self._update_call_step(
                    trace_run(event.step),
                    trace_index(event.step) or 0,
                    f"{event.kind} running",
                )
            return
        if event.type == "step_end":
            self._update_step(cast(StepEnd, event))
            return
        if event.type == "run_end":
            event = cast(RunEnd, event)
            if event.run == self._run_id:
                self._finished = True
                self._render()
                self.finish()
                return
            call = self._call_for_run(event.run)
            if call is not None:
                call.status = self._status_word(event.status)
                self._render()

    def finish(self) -> None:
        if self._live is None:
            return
        self._live.stop()
        self._live = None

    def interrupt(self) -> None:
        self.finish()

    def _update_step(self, event: StepEnd) -> None:
        payload = event.detail
        root, _, indexes = event.step.partition("/")
        first_index = indexes.split("/", 1)[0] if indexes else ""
        stage_key = f"{root}/{first_index}" if first_index else event.step
        if trace_run(event.step) == self._run_id and stage_key in self._stages:
            stage = self._ensure_stage({**payload, "_step": event.step})
            stage.status = self._status_word(event.status)
            items = self._int_payload(payload.get("items"))
            shape = self._text(payload.get("shape"))
            if shape == "list":
                stage.output_shape = (
                    self._shape_label({"count": items}) if items is not None else "list"
                )
                stage.item_total = items
            elif shape == "item":
                stage.output_shape = "1 item"
            elif shape == "none":
                stage.output_shape = "unset"
            self._render()
            return
        self._update_call_step(
            trace_run(event.step),
            trace_index(event.step) or 0,
            f"{event.kind} {self._status_word(event.status)}",
        )

    def _ensure_stage(self, payload: Mapping[str, object]) -> _StageProgress:
        ctx = self._context(payload)
        step = self._text(ctx.get("_step")) or ""
        root, _, indexes = step.partition("/")
        first_index = indexes.split("/", 1)[0] if indexes else ""
        key = f"{root}/{first_index}" if first_index else step or "stage"
        stage = self._stages.get(key)
        if stage is None:
            stage = _StageProgress(key=key)
            self._stages[key] = stage
            self._stage_order.append(key)
        stage.index = self._int_payload(first_index) if first_index else stage.index
        stage.kind = str(ctx.get("statement") or stage.kind)
        target = next(
            (
                self._text(ctx.get(name))
                for name in ("runnable", "predicate", "scorer", "agent")
                if self._text(ctx.get(name))
            ),
            None,
        )
        count = self._int_payload(ctx.get("count"))
        title = " ".join(
            item
            for item in (
                stage.kind,
                str(count) if count is not None else "",
                target or "",
            )
            if item
        )
        if title:
            stage.title = title
        stage.parallelism = (
            self._int_payload(ctx.get("par"))
            or self._int_payload(ctx.get("parallelism"))
            or stage.parallelism
        )
        if input_preview := ctx.get("input_preview"):
            stage.input_shape = self._shape_label(input_preview)
        if item_count := self._int_payload(ctx.get("item_count")):
            stage.item_total = item_count
        return stage

    def _ensure_call(
        self,
        *,
        run_id: str,
        stage: _StageProgress,
        target_label: str,
        payload: Mapping[str, object],
    ) -> _CallProgress:
        call_key = self._run_call_keys.get(run_id, run_id)
        ctx = self._context(payload)
        call = self._calls.get(call_key)
        if call is None:
            call = _CallProgress(
                key=call_key,
                stage_key=stage.key,
                label=self._call_label(target_label, ctx),
                run_id=run_id,
            )
            self._calls[call_key] = call
            self._run_call_keys[run_id] = call_key
            if call_key not in stage.calls:
                stage.calls.append(call_key)
        call.item_index = (
            self._int_payload(ctx.get("item_index"))
            if ctx.get("item_index") is not None
            else call.item_index
        )
        call.item_count = self._int_payload(ctx.get("item_count")) or call.item_count
        call.lane_index = (
            self._int_payload(ctx.get("lane_index"))
            if ctx.get("lane_index") is not None
            else call.lane_index
        )
        call.parallelism = self._int_payload(ctx.get("parallelism")) or call.parallelism
        if call.parallelism is not None:
            stage.parallelism = call.parallelism
        if call.item_count is not None:
            stage.item_total = call.item_count
        return call

    def _update_call_step(self, run_id: str, step_index: int, text: str) -> None:
        call = self._call_for_run(run_id)
        if call is None:
            return
        call.steps[step_index] = text
        self._render()

    def _call_for_run(self, run_id: str) -> _CallProgress | None:
        key = self._run_call_keys.get(run_id)
        return self._calls.get(key) if key is not None else None

    def _context(self, payload: Mapping[str, object]) -> dict[str, object]:
        ctx = dict(payload)
        placement = self._mapping(payload.get("placement"))
        if placement:
            ctx.setdefault("item_index", placement.get("item"))
            ctx.setdefault("item_count", placement.get("items"))
            ctx.setdefault("lane_index", placement.get("lane"))
            ctx.setdefault("parallelism", placement.get("lanes"))
        return ctx

    def _mapping(self, value: object) -> Mapping[str, object]:
        return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}

    def _text(self, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _call_label(self, target_label: str, ctx: Mapping[str, object]) -> str:
        item_index = self._int_payload(ctx.get("item_index"))
        item_count = self._int_payload(ctx.get("item_count"))
        target = target_label.replace(":", " ", 1)
        if item_index is not None:
            item = (
                f"item {item_index + 1}/{item_count}"
                if item_count
                else f"item {item_index + 1}"
            )
            return f"{item} · {target}"
        return target

    def _status_word(self, status: str) -> str:
        return "done" if status == "finished" else status

    def _stage_label(self, stage: _StageProgress) -> str:
        index = "?"
        if stage.index is not None:
            index = str(stage.index + 1)
        title = self._truncate(stage.title, 56 if self._verbosity == 0 else 84)
        return f"[{index}] {title}"

    def _stage_tail(self, stage: _StageProgress) -> str:
        lanes = (
            f"{stage.parallelism} lanes"
            if stage.parallelism and stage.parallelism > 1
            else ""
        )
        if stage.status == "done" and (stage.input_shape or stage.output_shape):
            shape = f"{stage.input_shape or '?'} -> {stage.output_shape or '?'}"
            return " · ".join(item for item in (shape, lanes) if item)
        done = self._stage_done_count(stage)
        failed = self._stage_failed_count(stage)
        if stage.item_total is not None:
            progress = f"{done}/{stage.item_total} items"
            if failed:
                progress = f"{progress} · {failed} failed"
            return " · ".join(item for item in (progress, lanes) if item)
        if failed:
            return f"{failed} failed"
        return "running"

    def _stage_done_count(self, stage: _StageProgress) -> int:
        return sum(
            1
            for call in self._stage_calls(stage)
            if call.status in {"done", "failed", "canceled"}
        )

    def _stage_failed_count(self, stage: _StageProgress) -> int:
        return sum(1 for call in self._stage_calls(stage) if call.status == "failed")

    def _stage_calls(self, stage: _StageProgress) -> list[_CallProgress]:
        calls = [self._calls[key] for key in stage.calls if key in self._calls]
        return sorted(
            calls,
            key=lambda call: (
                call.lane_index if call.lane_index is not None else 999_999,
                call.item_index if call.item_index is not None else 999_999,
                call.run_id,
            ),
        )

    def _lane_calls(self, stage: _StageProgress) -> dict[int, list[_CallProgress]]:
        lanes: dict[int, list[_CallProgress]] = {}
        for call in self._stage_calls(stage):
            lane = call.lane_index if call.lane_index is not None else 0
            lanes.setdefault(lane, []).append(call)
        return lanes

    def _render_lines(self) -> list[str]:
        lines = [self._title or f"Running {self._executable_name}"]
        for stage_key in self._stage_order:
            stage = self._stages[stage_key]
            tail = self._stage_tail(stage)
            line = self._stage_label(stage)
            if tail:
                line = f"{line} · {tail}"
            lines.append(line)
            if self._verbosity <= 0:
                continue
            if stage.parallelism and stage.parallelism > 1:
                lanes = self._lane_calls(stage)
                for lane_index in range(stage.parallelism):
                    calls = lanes.get(lane_index, [])
                    lane_done = sum(
                        1
                        for call in calls
                        if call.status in {"done", "failed", "canceled"}
                    )
                    lines.append(
                        f"  lane {lane_index + 1}/{stage.parallelism:<3} {lane_done}/{len(calls)} calls"
                    )
                    if self._verbosity <= 1:
                        continue
                    for call in calls:
                        lines.extend(
                            self._render_call(
                                call, indent="    ", include_steps=self._verbosity >= 3
                            )
                        )
                continue
            for call in self._stage_calls(stage):
                lines.extend(
                    self._render_call(
                        call, indent="  ", include_steps=self._verbosity >= 2
                    )
                )
        if self._finished:
            failed = sum(1 for call in self._calls.values() if call.status == "failed")
            lines.append(
                f"Done · {len(self._stage_order)} stages · {len(self._calls)} calls · {failed} failed"
            )
        return lines

    def _render_call(
        self, call: _CallProgress, *, indent: str, include_steps: bool
    ) -> list[str]:
        prefix = (
            "✓" if call.status == "done" else "✗" if call.status == "failed" else "…"
        )
        lines = [f"{indent}{prefix} {call.label} · {call.run_id} {call.status}"]
        if include_steps:
            for _index, text in sorted(call.steps.items()):
                lines.append(f"{indent}  - {text}")
        return lines

    def _shape_label(self, preview: object) -> str:
        if isinstance(preview, Mapping):
            preview = cast(Mapping[str, object], preview)
            count = self._int_payload(preview.get("count"))
            if count is not None:
                return "1 item" if count == 1 else f"{count} items"
            if preview.get("type") == "list":
                count = self._int_payload(preview.get("count"))
                if count is not None:
                    return "1 item" if count == 1 else f"{count} items"
            if preview.get("type") == "object":
                return "object"
        if preview is None:
            return "unset"
        return "1 item"

    def _int_payload(self, value: object) -> int | None:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    def _truncate(self, text: str, width: int) -> str:
        text = " ".join(text.split())
        if len(text) <= width:
            return text
        return f"{text[: max(width - 1, 0)].rstrip()}…"

    def _render(self) -> None:
        if not self._render_enabled:
            return
        body = "\n".join(self._render_lines())
        text = Text(body, style="dim")
        if self._live is None:
            self._live = Live(
                text,
                console=self._console,
                refresh_per_second=10,
                transient=False,
            )
            self._live.start(refresh=True)
            return
        self._live.update(text, refresh=True)
