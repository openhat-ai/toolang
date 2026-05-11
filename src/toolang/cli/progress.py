"""CLI rendering for progress events."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import TextIO

from ..progress import ProgressEvent, ProgressSink


class CliProgress:
    """Render progress updates to stderr without affecting stdout contracts."""

    def __init__(self, *, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stderr
        self._items: dict[str, _ProgressItem] = {}
        self._aliases: dict[str, str] = {}
        self._prepare: dict[str, str] = {}
        self._live = bool(getattr(self._stream, "isatty", lambda: False)())
        self._rendered_lines = 0
        self._printed = False

    def __call__(self, event: ProgressEvent) -> None:
        self._record(event)
        if self._live:
            self._render_live()
        self._printed = True

    def finish(self) -> None:
        if not self._printed:
            return
        if self._live and self._rendered_lines:
            self._clear_live()
        for line in self._lines():
            print(line, file=self._stream)
        print(self._summary_line(), file=self._stream, flush=True)

    def _render_live(self) -> None:
        if self._rendered_lines:
            self._clear_live()
        lines = (*self._lines(), self._summary_line())
        for line in lines:
            print(line, file=self._stream)
        self._stream.flush()
        self._rendered_lines = len(lines)

    def _clear_live(self) -> None:
        print(f"\x1b[{self._rendered_lines}F", end="", file=self._stream)
        for _ in range(self._rendered_lines):
            print("\x1b[2K", end="", file=self._stream)
            print("\x1b[1E", end="", file=self._stream)
        print(f"\x1b[{self._rendered_lines}F", end="", file=self._stream)
        self._rendered_lines = 0

    def _record(self, event: ProgressEvent) -> None:
        if event.phase.startswith("agent."):
            self._record_agent(event)
            return
        if event.phase.startswith("cap."):
            self._record_cap(event)
            return
        if event.phase.startswith("prepare.") or event.phase.startswith("startup."):
            self._prepare[event.phase] = event.status

    def _record_agent(self, event: ProgressEvent) -> None:
        step = event.phase.removeprefix("agent.")
        item = self._items.setdefault(
            "agent",
            _ProgressItem(kind="agent", title="Agent", sort_key=("0", "agent")),
        )
        item.steps[step] = event.status
        if step == "materialize" and event.detail:
            item.name = event.detail
        elif event.detail:
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
        return tuple(
            _format_item(item)
            for item in sorted(self._items.values(), key=lambda value: value.sort_key)
        )

    def _summary_line(self) -> str:
        agent_count = sum(1 for item in self._items.values() if item.kind == "agent")
        cap_items = [item for item in self._items.values() if item.kind != "agent"]
        failed = sum(1 for item in self._items.values() if _item_status(item) == "failed")
        running = sum(1 for item in self._items.values() if _item_status(item) == "running")
        prepare_status = _aggregate_status(tuple(self._prepare.values())) if self._prepare else "skipped"
        if failed:
            return f"Progress: {agent_count} agent, {len(cap_items)} caps, {failed} failed"
        if running:
            return f"Progress: {agent_count} agent, {len(cap_items)} caps, {running} running"
        prepare_text = "" if prepare_status == "skipped" else f", prepare {prepare_status}"
        return f"Progress: {agent_count} agent, {len(cap_items)} caps{prepare_text}"


def make_cli_progress() -> CliProgress:
    """Return the default CLI progress sink."""

    return CliProgress()


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
    steps = ", ".join(_completed_step_names(item))
    suffix = f" {steps}" if steps else ""
    if item.detail:
        suffix = f"{suffix} ({item.detail})"
    return f"{marker} {item.title} {name}{suffix}"


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
    if all(status == "skipped" for status in statuses):
        return "skipped"
    return "ok"


def _status_marker(status: str) -> str:
    marker = {
        "running": "...",
        "ok": "ok",
        "failed": "failed",
        "skipped": "skipped",
    }[status]
    return marker


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
