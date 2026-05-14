"""CLI rendering for progress events."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
from threading import RLock
import time
from typing import TextIO

from rich import box
from rich.console import Console
from rich.live import Live
from rich.table import Table
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
        self._agent_name: str | None = None
        self._live = bool(getattr(self._stream, "isatty", lambda: False)()) if live is None else live
        self._live_display: Live | None = None
        self._printed = False
        self._started_at = time.monotonic()
        self._lock = RLock()

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            self._record(event)
            if self._live:
                self._render_live()
            self._printed = True

    def finish(self, *, details: bool = True) -> None:
        with self._lock:
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

    def _render_live(self) -> None:
        if not self._has_visible_items():
            return
        table = self._table()
        if self._live_display is None:
            self._live_display = Live(
                table,
                console=self._console,
                refresh_per_second=10,
                transient=True,
            )
            self._live_display.start(refresh=True)
            return
        self._live_display.update(table, refresh=True)

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
        self._prepare[event.phase] = event.status
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
                name=_display_name(event_ref or "agent"),
            ),
        )
        item.steps[step] = event.status
        if step == "materialize" and event.detail:
            item.name = event.detail
        elif event.detail and item.ref is None:
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
            ref = event.detail
        item = self._items.setdefault(
            key,
            _ProgressItem(
                kind=kind,
                title=kind.capitalize(),
                sort_key=("1", kind, _display_name(ref), ref),
            ),
        )
        item.kind = kind
        item.title = kind.capitalize()
        item.ref = ref
        item.name = _display_name(ref)
        item.sort_key = ("1", kind, item.name or "", ref)
        item.steps[step] = event.status
        if step == "fetch" and event.detail:
            item.detail = event.detail

    def _lines(self) -> tuple[str, ...]:
        lines: list[str] = []
        for group in self._item_groups():
            lines.extend(_format_item(item) for item in group)
        return tuple(lines)

    def _summary_line(self) -> str:
        agent_items = [item for item in self._items.values() if item.kind == "agent"]
        cap_items = [item for item in self._items.values() if item.kind != "agent"]
        failed = sum(1 for item in cap_items if _item_status(item) == "failed")
        running = sum(1 for item in cap_items if _item_status(item) == "running")
        pending = sum(1 for item in cap_items if _item_status(item) == "pending")
        elapsed = _format_elapsed(time.monotonic() - self._started_at)
        agent_name = self._display_agent_name()
        if not cap_items and agent_items:
            agent_status = _aggregate_status(tuple(_item_status(item) for item in agent_items))
            if agent_status == "failed":
                return f"Agent {agent_name} failed: {elapsed}"
            if agent_status in {"running", "pending"}:
                return f"Agent {agent_name} preparing: {elapsed}"
            return f"Agent {agent_name} ready: {elapsed}"
        prepare_status = _aggregate_status(tuple(self._prepare.values())) if self._prepare else "skipped"
        if failed:
            return f"Agent {agent_name} prepare failed: {failed}/{len(cap_items)} caps, {elapsed}"
        if running:
            return f"Agent {agent_name} preparing: {len(cap_items)} caps, {running} running, {elapsed}"
        if pending:
            return f"Agent {agent_name} preparing: {len(cap_items)} caps, {pending} pending, {elapsed}"
        if prepare_status in {"ok", "skipped"}:
            return f"Agent {agent_name} prepared: {len(cap_items)} caps, {elapsed}"
        return f"Agent {agent_name} prepared: {len(cap_items)} caps, {prepare_status}, {elapsed}"

    def _print_summary(self) -> None:
        summary = self._summary_line()
        if self._live:
            self._console.print(Text(summary, style="dim"))
            return
        print(summary, file=self._stream, flush=True)

    def _table(self) -> Table:
        table = Table(
            box=box.SIMPLE,
            expand=False,
            show_edge=False,
            padding=(0, 1),
        )
        table.add_column("Status", no_wrap=True)
        table.add_column("Kind", no_wrap=True)
        table.add_column("Name", no_wrap=True)
        table.add_column("Info", overflow="fold")
        groups = self._item_groups()
        for group_index, group in enumerate(groups):
            if group_index:
                table.add_section()
            for item in group:
                table.add_row(
                    _status_text(_item_status(item)),
                    item.title,
                    item.name or _display_name(item.ref or item.title),
                    _item_info(item),
                )
        table.add_section()
        table.add_row("", Text("Summary", style="dim"), "", Text(self._summary_line(), style="dim"))
        return table

    def _has_visible_items(self) -> bool:
        return bool(self._items)

    def _has_output(self) -> bool:
        return self._has_visible_items() or self._agent_name is not None

    def _display_agent_name(self) -> str:
        if self._agent_name:
            return self._agent_name
        agent_item = self._items.get("agent")
        if agent_item is not None and agent_item.name:
            return agent_item.name
        return "agent"

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


def _format_item(item: _ProgressItem) -> str:
    marker = _status_marker(_item_status(item))
    name = item.name or _display_name(item.ref or item.title)
    info = _item_info(item)
    suffix = f" {info}" if info else ""
    return f"{marker} {item.title} {name}{suffix}"


def _item_info(item: _ProgressItem) -> str:
    steps = ", ".join(_completed_step_names(item))
    if item.detail:
        return f"{steps} ({item.detail})" if steps else f"({item.detail})"
    return steps


def _completed_step_names(item: _ProgressItem) -> tuple[str, ...]:
    ordered_steps = ("resolve", "fetch", "materialize")
    return tuple(step for step in ordered_steps if item.steps.get(step) == "ok")


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


def _status_marker(status: str) -> str:
    marker = {
        "pending": "pending",
        "running": "...",
        "ok": "ok",
        "failed": "failed",
        "skipped": "skipped",
    }[status]
    return marker


def _status_text(status: str) -> Text:
    marker = _status_marker(status)
    style = {
        "pending": "dim",
        "running": "cyan",
        "ok": "green",
        "failed": "red",
        "skipped": "dim",
    }[status]
    return Text(marker, style=style)


def _format_elapsed(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f} secs"
    return f"{seconds:.0f} secs"


def _parse_cap_event(event: ProgressEvent) -> tuple[str, str] | None:
    parts = event.id.split(":", 2)
    if len(parts) != 3:
        return None
    prefix, kind, ref = parts
    if prefix not in {"cap.resolve", "cap.fetch", "cap.config"}:
        return None
    return kind, ref


def _display_name(ref: str) -> str:
    if ref.startswith("github://"):
        path = ref.split("://", 1)[1].split("/", 2)[-1]
        path = path.rsplit("@", 1)[0]
        return Path(path).stem
    return Path(ref).stem or ref
