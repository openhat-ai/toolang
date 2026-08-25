"""CLI rendering for progress events."""

from __future__ import annotations

from dataclasses import dataclass, field
import sys
from threading import RLock
import time
from typing import TextIO

from rich.console import Console
from rich.live import Live
from rich.text import Text

from ...common.events import ProgressEvent
from ...common.progress import ProgressSink


class CliProgress:
    """Render progress updates to stderr without affecting stdout contracts."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        live: bool | None = None,
        show_cached_prepare: bool = False,
        show_materialize_summary: bool = False,
        prepare_summary_label: str = "Prepared",
    ) -> None:
        self._stream = stream or sys.stderr
        stream_is_tty = bool(getattr(self._stream, "isatty", lambda: False)())
        force_terminal = (
            True if live is True or (live is False and stream_is_tty) else None
        )
        color_system = "standard" if force_terminal else "auto"
        self._console = Console(
            file=self._stream,
            force_terminal=force_terminal,
            color_system=color_system,
            highlight=False,
        )
        self._items: dict[str, _ProgressItem] = {}
        self._aliases: dict[str, str] = {}
        self._prepare: dict[str, str] = {}
        self._prepare_details: dict[str, str] = {}
        self._agent_name: str | None = None
        self._live = stream_is_tty if live is None else live
        self._live_display: Live | None = None
        self._printed = False
        self._finished = False
        self._interrupted = False
        self._show_cached_prepare = show_cached_prepare
        self._show_materialize_summary = show_materialize_summary
        self._prepare_summary_label = prepare_summary_label
        self._prepare_total: int | None = None
        self._materialized_keys: set[str] = set()
        self._post_resolve_started_at: float | None = None
        self._materialize_started_at: float | None = None
        self._materialize_finished_at: float | None = None
        self._started_at = time.monotonic()
        self._lock = RLock()

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            if self._finished:
                return
            self._record(event)
            if self._live:
                self._render_live()
            self._printed = True

    def finish(self, *, details: bool = True) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            if not self._printed:
                return
            if self._live_display is not None:
                self._live_display.stop()
                self._live_display = None
            if not self._has_output():
                return
            if details:
                for line in self._lines():
                    print(line, file=self._stream)
            self._print_summary()
            self._reset_terminal_style()

    def interrupt(self) -> None:
        with self._lock:
            self._interrupted = True
            self.finish(details=False)

    def set_prepare_total(self, total: int) -> None:
        with self._lock:
            self._prepare_total = total

    def _render_live(self) -> None:
        if not self._has_visible_items():
            return
        renderable = self._live_text()
        if self._live_display is None:
            self._live_display = Live(
                renderable,
                console=self._console,
                refresh_per_second=10,
                transient=True,
            )
            self._live_display.start(refresh=True)
            return
        self._live_display.update(renderable, refresh=True)

    def _record(self, event: ProgressEvent) -> None:
        if event.phase.startswith("agent."):
            self._record_agent(event)
            return
        if event.phase.startswith("cap."):
            self._record_cap(event)
            return
        if event.phase.startswith("prepare."):
            self._record_prepare(event)

    def _record_prepare(self, event: ProgressEvent) -> None:
        key = event.id if event.phase == "prepare.visibility" else event.phase
        self._prepare[key] = event.status
        if event.detail:
            self._prepare_details[key] = event.detail
        if event.status == "running" and event.detail:
            self._agent_name = event.detail

    def _record_agent(self, event: ProgressEvent) -> None:
        step = event.phase.removeprefix("agent.")
        event_ref = event.id.split(":", 1)[1] if ":" in event.id else event.detail
        item = self._items.setdefault(
            "agent",
            _ProgressItem(
                kind="agent",
                title="Agent",
                sort_key=("0", "agent"),
                name=event_ref or "agent",
            ),
        )
        item.steps[step] = event.status
        if event.detail:
            item.step_details[step] = event.detail
        if event.detail and item.ref is None:
            item.ref = event.detail

    def _record_cap(self, event: ProgressEvent) -> None:
        parsed = _parse_cap_event(event)
        if parsed is None:
            return
        kind, ref = parsed
        step = event.phase.removeprefix("cap.")
        if step == "config":
            return
        key = self._aliases.get(ref, f"cap:{kind}:{ref}")
        if step == "resolve" and event.status == "ok" and event.detail:
            canonical_key = f"cap:{kind}:{event.detail}"
            self._aliases[ref] = canonical_key
            existing = self._items.pop(key, None)
            if existing is not None and canonical_key not in self._items:
                self._items[canonical_key] = existing
            key = canonical_key
        item = self._items.setdefault(
            key,
            _ProgressItem(
                kind=kind,
                title=kind.capitalize(),
                name=ref,
                sort_key=("1", kind, ref),
            ),
        )
        item.kind = kind
        item.title = kind.capitalize()
        if item.ref is None:
            item.ref = ref
        if item.name is None:
            item.name = ref
        item.sort_key = ("1", kind, item.name or "", item.ref or "")
        item.steps[step] = event.status
        if event.detail:
            item.step_details[step] = event.detail
            if step == "fetch" and event.status == "ok":
                item.detail = event.detail
        if (
            step in {"fetch", "extract", "materialize"}
            and self._post_resolve_started_at is None
        ):
            self._post_resolve_started_at = time.monotonic()
        if step == "materialize":
            if event.status == "running" and self._materialize_started_at is None:
                self._materialize_started_at = time.monotonic()
            if event.status == "ok":
                self._materialized_keys.add(key)
                self._materialize_finished_at = time.monotonic()
            if event.status == "failed":
                self._materialize_finished_at = time.monotonic()

    def _lines(self) -> tuple[str, ...]:
        lines: list[str] = []
        for group in self._item_groups():
            lines.extend(_format_item(item) for item in group)
        return tuple(lines)

    def _summary_line(self) -> str:
        lines = self._summary_lines()
        return lines[0] if lines else ""

    def _summary_lines(self) -> tuple[str, ...]:
        agent_items = [item for item in self._items.values() if item.kind == "agent"]
        cap_items = [item for item in self._items.values() if item.kind != "agent"]
        if not cap_items and self._prepare_is_cached():
            if not self._show_cached_prepare:
                return ()
            elapsed = _format_elapsed(time.monotonic() - self._started_at)
            return (
                f"{self._prepare_summary_label} {self._cap_count_label()} from cache in {elapsed}",
            )
        failed = sum(1 for item in cap_items if _item_status(item) == "failed")
        running = sum(1 for item in cap_items if _item_status(item) == "running")
        pending = sum(1 for item in cap_items if _item_status(item) == "pending")
        elapsed = self._prepare_elapsed()
        prepare_status = (
            _aggregate_status(tuple(self._prepare.values()))
            if self._prepare
            else "skipped"
        )
        if self._interrupted:
            if cap_items or self._prepare:
                return ("Prepare caps interrupted",)
            if agent_items:
                return ("Fetch agent interrupted",)
            return ()
        if self._prepare:
            total = (
                self._prepare_total
                if self._prepare_total is not None
                else len(cap_items)
            )
            if failed:
                return (f"Failed {failed}/{total} caps",)
            if running:
                return (f"Preparing {total} caps: {running} running, {elapsed}",)
            if pending:
                return (f"Preparing {total} caps: {pending} pending, {elapsed}",)
            if prepare_status in {"ok", "skipped"}:
                return self._with_materialize_summary(
                    f"{self._prepare_summary_label} {self._cap_count_label(total)} in {elapsed}"
                )
            if prepare_status in {"running", "pending"}:
                return (f"Preparing {total} caps: {elapsed}",)
            return self._with_materialize_summary(
                f"{self._prepare_summary_label} {self._cap_count_label(total)} in {elapsed}"
            )
        if not cap_items and agent_items:
            item = agent_items[0]
            agent_status = _aggregate_status(
                tuple(_item_status(item) for item in agent_items)
            )
            if agent_status == "failed":
                detail = _failed_detail(item)
                suffix = f": {detail}" if detail else ""
                if _first_step_with_status(item, "failed") == "resolve":
                    return (f"Resolve agent failed{suffix}",)
                return (f"Fetch agent failed{suffix}",)
            if agent_status in {"running", "pending"}:
                return (f"Fetching 1 agent: {_agent_progress_word(item)}, {elapsed}",)
            return (f"Fetched 1 agent in {elapsed}",)
        if failed:
            return (f"Failed {failed}/{len(cap_items)} caps",)
        if running:
            return (f"Preparing {len(cap_items)} caps: {running} running, {elapsed}",)
        if pending:
            return (f"Preparing {len(cap_items)} caps: {pending} pending, {elapsed}",)
        if prepare_status in {"ok", "skipped"}:
            return self._with_materialize_summary(
                f"{self._prepare_summary_label} {self._cap_count_label(len(cap_items))} in {elapsed}"
            )
        return self._with_materialize_summary(
            f"{self._prepare_summary_label} {self._cap_count_label(len(cap_items))} in {elapsed}"
        )

    def _with_materialize_summary(self, summary: str) -> tuple[str, ...]:
        if not self._show_materialize_summary or not self._materialized_keys:
            return (summary,)
        started_at = (
            self._post_resolve_started_at
            or self._materialize_started_at
            or self._started_at
        )
        finished_at = self._materialize_finished_at or time.monotonic()
        elapsed = _format_elapsed(max(finished_at - started_at, 0))
        return (summary, f"Updated {len(self._materialized_keys)} caps in {elapsed}")

    def _prepare_elapsed(self) -> str:
        if (
            self._show_materialize_summary
            and self._materialized_keys
            and self._post_resolve_started_at is not None
        ):
            return _format_elapsed(
                max(self._post_resolve_started_at - self._started_at, 0)
            )
        return _format_elapsed(time.monotonic() - self._started_at)

    def _cap_count_label(self, total: int | None = None) -> str:
        value = self._prepare_total if total is None else total
        if value is None:
            return "caps"
        return f"{value} caps"

    def _print_summary(self) -> None:
        summaries = self._summary_lines()
        if not summaries:
            return
        for summary in summaries:
            self._console.print(Text(summary, style="dim"))

    def _reset_terminal_style(self) -> None:
        if not self._console.is_terminal:
            return
        self._stream.write("\x1b[0m")
        self._stream.flush()

    def _live_text(self) -> Text:
        text = Text()
        agent_items = [item for item in self._items.values() if item.kind == "agent"]
        cap_items = [item for item in self._items.values() if item.kind != "agent"]
        if (
            agent_items
            and not cap_items
            and not self._prepare
            and not self._agent_stage_uses_summary(agent_items[0])
        ):
            text.append(f"{_format_item(agent_items[0])}\n", style="dim")
            return text
        for summary in self._summary_lines():
            text.append(f"{summary}\n", style="dim")
        for group in self._item_groups():
            for item in group:
                if item.kind == "agent":
                    continue
                text.append(f"+ {_format_item(item)}\n", style="dim")
        return text

    def _agent_stage_uses_summary(self, item: _ProgressItem) -> bool:
        if self._interrupted or _item_status(item) == "failed":
            return True
        return item.steps.get("fetch") == "ok" or item.steps.get("materialize") == "ok"

    def _has_visible_items(self) -> bool:
        return bool(self._items)

    def _has_output(self) -> bool:
        return self._has_visible_items() or self._agent_name is not None

    def _prepare_is_cached(self) -> bool:
        visibility_phases = tuple(
            phase for phase in self._prepare if phase.startswith("prepare.visibility:")
        )
        return bool(visibility_phases) and all(
            self._prepare.get(phase) == "ok"
            and self._prepare_details.get(phase) == "cached"
            for phase in visibility_phases
        )

    def _item_groups(self) -> tuple[tuple[_ProgressItem, ...], ...]:
        cap_items = tuple(
            sorted(
                (item for item in self._items.values() if item.kind != "agent"),
                key=lambda value: value.sort_key,
            )
        )
        if cap_items:
            return (cap_items,)
        agent_items = tuple(
            sorted(
                (item for item in self._items.values() if item.kind == "agent"),
                key=lambda value: value.sort_key,
            )
        )
        return (agent_items,) if agent_items else ()


def make_cli_progress(
    *,
    live: bool | None = None,
    show_cached_prepare: bool = False,
    show_materialize_summary: bool = False,
    prepare_summary_label: str = "Prepared",
) -> CliProgress:
    """Return the default CLI progress sink."""

    return CliProgress(
        live=live,
        show_cached_prepare=show_cached_prepare,
        show_materialize_summary=show_materialize_summary,
        prepare_summary_label=prepare_summary_label,
    )


def as_progress_sink(progress: CliProgress | None) -> ProgressSink | None:
    """Expose a CLI progress renderer as a core progress sink."""

    return progress


@dataclass(slots=True)
class _ProgressItem:
    kind: str
    title: str
    sort_key: tuple[str, ...]
    name: str | None = None
    ref: str | None = None
    detail: str | None = None
    steps: dict[str, str] = field(default_factory=dict)
    step_details: dict[str, str] = field(default_factory=dict)


def _format_item(item: _ProgressItem) -> str:
    name = item.name or item.ref or item.title
    status, info = _item_state(item)
    if info == name or (item.kind != "agent" and info == item.ref):
        info = ""
    suffix = f": {info}" if info else ""
    return f"{item.kind} {name} {status}{suffix}"


def _item_state(item: _ProgressItem) -> tuple[str, str]:
    failed_step = _first_step_with_status(item, "failed")
    if failed_step is not None:
        return "failed", item.step_details.get(failed_step, "")
    running_step = _first_step_with_status(item, "running")
    if running_step is not None:
        return _running_word(running_step), item.step_details.get(running_step, "")
    pending_step = _first_step_with_status(item, "pending")
    if pending_step is not None:
        return "pending", item.step_details.get(pending_step, "")
    if item.kind == "agent":
        if item.steps.get("materialize") == "ok" or item.steps.get("fetch") == "ok":
            return "fetched", ""
        if item.steps.get("resolve") == "ok":
            return "resolved", item.step_details.get("resolve", "")
    if item.steps.get("materialize") == "ok":
        return "materialized", ""
    if item.steps.get("extract") == "ok":
        return "extracted", ""
    if item.steps.get("fetch") == "ok":
        return "fetched", item.detail or ""
    if item.steps.get("resolve") == "ok":
        return "resolved", item.step_details.get("resolve", "")
    if item.steps:
        return _item_status(item), ""
    return "pending", ""


def _first_step_with_status(item: _ProgressItem, status: str) -> str | None:
    for step in ("resolve", "fetch", "extract", "materialize"):
        if item.steps.get(step) == status:
            return step
    return None


def _running_word(step: str) -> str:
    return {
        "resolve": "resolving",
        "fetch": "fetching",
        "extract": "extracting",
        "materialize": "materializing",
    }.get(step, "running")


def _agent_progress_word(item: _ProgressItem) -> str:
    running_step = _first_step_with_status(item, "running")
    if running_step == "resolve":
        return "resolving"
    if running_step == "fetch":
        return "fetching"
    if running_step == "extract":
        return "extracting"
    if running_step == "materialize":
        return "materializing"
    pending_step = _first_step_with_status(item, "pending")
    if pending_step is not None:
        return "pending"
    return "running"


def _failed_detail(item: _ProgressItem) -> str:
    failed_step = _first_step_with_status(item, "failed")
    if failed_step is None:
        return ""
    return item.step_details.get(failed_step, "")


def _item_status(item: _ProgressItem) -> str:
    return _aggregate_status(tuple(item.steps.values()))


def _aggregate_status(statuses: tuple[str, ...]) -> str:
    if not statuses:
        return "running"
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status == "running" for status in statuses):
        return "running"
    if any(status == "pending" for status in statuses):
        return "pending"
    if all(status == "skipped" for status in statuses):
        return "skipped"
    return "ok"


def _format_elapsed(seconds: float) -> str:
    if seconds < 1:
        return f"{max(round(seconds * 1000), 1)}ms"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def _parse_cap_event(event: ProgressEvent) -> tuple[str, str] | None:
    parts = event.id.split(":", 2)
    if len(parts) != 3:
        return None
    prefix, kind, ref = parts
    if prefix not in {
        "cap.resolve",
        "cap.fetch",
        "cap.extract",
        "cap.materialize",
        "cap.config",
    }:
        return None
    return kind, ref
