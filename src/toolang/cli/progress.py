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

from ..progress import ProgressEvent, ProgressSink


class CliProgress:
    """Render progress updates to stderr without affecting stdout contracts."""

    def __init__(self, *, stream: TextIO | None = None, live: bool | None = None) -> None:
        self._stream = stream or sys.stderr
        self._console = Console(file=self._stream, force_terminal=live, highlight=False)
        self._items: dict[str, _ProgressItem] = {}
        self._aliases: dict[str, str] = {}
        self._prepare: dict[str, str] = {}
        self._prepare_details: dict[str, str] = {}
        self._agent_name: str | None = None
        self._live = bool(getattr(self._stream, "isatty", lambda: False)()) if live is None else live
        self._live_display: Live | None = None
        self._printed = False
        self._finished = False
        self._interrupted = False
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

    def interrupt(self) -> None:
        with self._lock:
            self._interrupted = True
            self.finish(details=False)

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
        if event.phase.startswith("prepare.") or event.phase.startswith("startup."):
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

    def _lines(self) -> tuple[str, ...]:
        lines: list[str] = []
        for group in self._item_groups():
            lines.extend(_format_item(item) for item in group)
        return tuple(lines)

    def _summary_line(self) -> str:
        agent_items = [item for item in self._items.values() if item.kind == "agent"]
        cap_items = [item for item in self._items.values() if item.kind != "agent"]
        if not cap_items and self._prepare_is_cached():
            return ""
        failed = sum(1 for item in cap_items if _item_status(item) == "failed")
        running = sum(1 for item in cap_items if _item_status(item) == "running")
        pending = sum(1 for item in cap_items if _item_status(item) == "pending")
        elapsed = _format_elapsed(time.monotonic() - self._started_at)
        prepare_status = _aggregate_status(tuple(self._prepare.values())) if self._prepare else "skipped"
        if self._interrupted:
            if cap_items or self._prepare:
                return "Prepare caps interrupted"
            if agent_items:
                return "Fetch agent interrupted"
            return ""
        if self._prepare:
            total = len(cap_items)
            if failed:
                return f"Failed {failed}/{total} caps"
            if running:
                return f"Preparing {total} caps: {running} running, {elapsed}"
            if pending:
                return f"Preparing {total} caps: {pending} pending, {elapsed}"
            if prepare_status in {"ok", "skipped"}:
                return f"Prepared {total} caps in {elapsed}"
            if prepare_status in {"running", "pending"}:
                return f"Preparing {total} caps: {elapsed}"
            return f"Prepared {total} caps in {elapsed}"
        if not cap_items and agent_items:
            agent_status = _aggregate_status(tuple(_item_status(item) for item in agent_items))
            if agent_status == "failed":
                item = agent_items[0]
                detail = _failed_detail(item)
                suffix = f": {detail}" if detail else ""
                if _first_step_with_status(item, "failed") == "resolve":
                    return f"Resolve agent failed{suffix}"
                return f"Fetch agent failed{suffix}"
            if agent_status in {"running", "pending"}:
                return f"Fetching 1 agent: {elapsed}"
            return f"Fetched 1 agent in {elapsed}"
        if failed:
            return f"Failed {failed}/{len(cap_items)} caps"
        if running:
            return f"Preparing {len(cap_items)} caps: {running} running, {elapsed}"
        if pending:
            return f"Preparing {len(cap_items)} caps: {pending} pending, {elapsed}"
        if prepare_status in {"ok", "skipped"}:
            return f"Prepared {len(cap_items)} caps in {elapsed}"
        return f"Prepared {len(cap_items)} caps in {elapsed}"

    def _print_summary(self) -> None:
        summary = self._summary_line()
        if not summary:
            return
        if self._live:
            self._console.print(Text(summary, style="dim"))
            return
        print(summary, file=self._stream, flush=True)

    def _live_text(self) -> Text:
        text = Text()
        summary = self._summary_line()
        if summary:
            text.append(f"{summary}\n", style="dim")
        for group in self._item_groups():
            for item in group:
                style = "dim" if item.kind != "agent" else ""
                prefix = " * " if item.kind != "agent" else ""
                text.append(f"{prefix}{_format_item(item)}\n", style=style)
        return text

    def _has_visible_items(self) -> bool:
        return bool(self._items)

    def _has_output(self) -> bool:
        return self._has_visible_items() or self._agent_name is not None

    def _prepare_is_cached(self) -> bool:
        visibility_phases = tuple(
            phase for phase in self._prepare if phase.startswith("prepare.visibility:")
        )
        return bool(visibility_phases) and all(
            self._prepare.get(phase) == "ok" and self._prepare_details.get(phase) == "cached"
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


def make_cli_progress(*, live: bool | None = None) -> CliProgress:
    """Return the default CLI progress sink."""

    return CliProgress(live=live)


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
    if info == name or info == item.ref:
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
        return "prepared", ""
    if item.steps.get("fetch") == "ok":
        return "fetched", item.detail or ""
    if item.steps.get("resolve") == "ok":
        return "resolved", item.step_details.get("resolve", "")
    if item.steps:
        return _item_status(item), ""
    return "pending", ""


def _first_step_with_status(item: _ProgressItem, status: str) -> str | None:
    for step in ("resolve", "fetch", "materialize"):
        if item.steps.get(step) == status:
            return step
    return None


def _running_word(step: str) -> str:
    return {
        "resolve": "resolving",
        "fetch": "fetching",
        "materialize": "materializing",
    }.get(step, "running")


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
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def _parse_cap_event(event: ProgressEvent) -> tuple[str, str] | None:
    parts = event.id.split(":", 2)
    if len(parts) != 3:
        return None
    prefix, kind, ref = parts
    if prefix not in {"cap.resolve", "cap.fetch", "cap.materialize", "cap.config"}:
        return None
    return kind, ref
