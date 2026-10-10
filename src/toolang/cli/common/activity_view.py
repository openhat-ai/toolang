"""Top views, row ownership, and terminal interaction; no execution or accounting."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import re
from typing import Literal, cast

from prompt_toolkit.keys import Keys
from rich.console import Group
from rich.text import Text

from toolang.execution.activity import ActivityQuery, duration
from .terminal_surfaces import DARK_TERMINAL_SURFACES, TerminalSurfaces
from toolang.execution.schemas import ActivityMetrics, ActivityNode, ActivitySnapshot

View = Literal["agent", "thread", "execution"]
Sort = Literal["activity", "spend", "cost", "time"]
RECENT = ("5m", "30m", "1h", "1d", "1w", "all")
SINCE = ("session", "1h", "1d", "1w", "all")


def since(value: str) -> str:
    query = ActivityQuery(value)
    if value in {"session", "all"} or query.window is not None:
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()


def elapsed(value: float | None) -> str:
    if value is None:
        return "-"
    seconds = max(0, int(value))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600:02}h"


def cost(metrics: ActivityMetrics) -> str:
    if metrics.cost is None:
        return "-"
    return f"${metrics.cost:.2f}"


def tokens(value: int | None) -> str:
    if value is None:
        return "-"
    for size, suffix in ((1_000_000_000, "G"), (1_000_000, "M"), (1000, "k")):
        if value >= size:
            return f"{value / size:.1f}{suffix}"
    return str(value)


def metrics_text(metrics: ActivityMetrics, *, exact: bool = False) -> str:
    number = (
        (lambda value: str(value) if value is not None else "-") if exact else tokens
    )
    return (
        f"MODEL {metrics.model if metrics.model is not None else '-'}  TOOL {metrics.tool if metrics.tool is not None else '-'}"
        f"  IN {number(metrics.input_tokens)}  CACHED {number(metrics.cached_tokens)}  OUT {number(metrics.output_tokens)}"
        f"  SPEND {cost(metrics)}  TIME+ {elapsed(metrics.time)}"
    )


def counts(active: int, failed: int, complete: bool = True) -> str:
    values = [f"{active} active"] if active else []
    if failed:
        values.append(f"{failed} failed")
    if not complete:
        return " · ".join(values) or "-"
    return " · ".join(values) or "idle"


def clean(value: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", value)


def node_summary(node: ActivityNode) -> str:
    summary = node.summary
    if node.kind == "run" and (
        len(summary) > 160 or re.search(r"[\n\r]|\*\*|^\s*#|`", summary)
    ):
        summary = ""
    title = node.title.removeprefix("agent::")
    label = title + (f" · {summary}" if summary else "")
    if node.stale:
        return f"stale · {label}"
    if node.status not in {"running", "idle"}:
        return f"{node.status} · {summary or title}"
    if node.kind == "step" and node.children:
        label += f" · {node.completed}/{node.children} completed"
    elif node.pending:
        label += f" · {node.pending} pending"
    return label


@dataclass
class Row:
    agent: str
    id: str
    node: ActivityNode | None
    stats: ActivityMetrics
    activity: str
    root: str = ""
    thread: str = ""
    parent: str | None = None
    depth: int = 0
    children: bool = False
    placeholder: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return self.agent, self.id


class Activity:
    def __init__(
        self,
        agent: str | None,
        *,
        view: View | None = None,
        tree: bool = False,
        sort: Sort = "activity",
        query: ActivityQuery | None = None,
        recent_label: str = "30m",
        surfaces: TerminalSurfaces = DARK_TERMINAL_SURFACES,
    ) -> None:
        self.surfaces = surfaces
        self.result_lines_key: tuple | None = None
        self.result_lines: list[Text] = []
        self.agent = agent
        self.view: View = view or ("thread" if agent else "agent")
        self.tree = tree
        self.sort = "spend" if sort == "cost" else sort
        self.column_widths: dict[str, int] = {}
        self.help = False
        self.query = query or ActivityQuery()
        self.recent_label = recent_label
        self.display_query = self.query
        self.attached_query = self.query
        self.selection_ancestors: list[tuple[str, str]] = []
        self.open_matches = False
        self.snapshots: dict[str, ActivitySnapshot] = {}
        self.pending: dict[str, ActivitySnapshot] = {}
        self.ready = False
        self.reconnecting = False
        self.selected: tuple[str, str] | None = None
        self.tree_selected: tuple[str, str] | None = None
        self.parents: dict[tuple[str, str], tuple[str, str]] = {}
        self.folded: set[tuple[str, str]] = set()
        self.offset = 0
        self.horizontal = 0
        self.details = False
        self.result_key: tuple[str, str, str] | None = None
        self.result_text = ""
        self.details_offset = 0
        self.details_page_size = 3
        self.details_selection: tuple[str, str] | None = None
        self.editor: str | None = None
        self.filter_active = self.query.active
        self.buffer = ""
        self.error = ""
        self.page_size = 15
        self.dirty = False

    def attach(self) -> None:
        self.pending.clear()
        self.attached_query = self.query

    def feed(self, event: str, data: dict) -> bool:
        if event == "activity_roster":
            if self.attached_query != self.query:
                return False
            agents = set(data["agents"])
            self.snapshots = {
                agent: page for agent, page in self.snapshots.items() if agent in agents
            }
            self.pending = {
                agent: page for agent, page in self.pending.items() if agent in agents
            }
            self._selection(self.rows())
            return True
        if event == "activity_page":
            page = ActivitySnapshot.model_validate(data)
            previous = self.pending.get(page.agent)
            if page.offset == 0:
                self.pending[page.agent] = page
            elif (
                previous
                and previous.revision == page.revision
                and previous.session == page.session
                and previous.next_offset == page.offset
            ):
                previous.roots.extend(page.roots)
                previous.paths.extend(page.paths)
                previous.next_offset = page.next_offset
                previous.complete &= page.complete
            else:
                raise ValueError("Activity pagination changed; reconnect required")
        elif event == "activity_checkpoint":
            if self.attached_query != self.query:
                self.pending.clear()
                return False
            agents = data["agents"]
            if set(agents) != set(self.pending) or any(
                page.next_offset is not None for page in self.pending.values()
            ):
                raise ValueError("Activity snapshot was incomplete")
            self.selection_ancestors = []
            ancestor = self.selected
            while ancestor and ancestor in self.parents:
                ancestor = self.parents[ancestor]
                self.selection_ancestors.append(ancestor)
            if data.get("replace", True):
                self.snapshots = self.pending
            else:
                self.snapshots.update(self.pending)
            self.pending = {}
            self.ready = True
            self.display_query = self.attached_query
            if self.open_matches:
                self.rows()
                for agent, snapshot in self.snapshots.items():
                    live = {node.id for node in snapshot.paths}
                    for root in snapshot.roots:
                        for match in root.matches:
                            if match not in live:
                                continue
                            parent = (agent, match)
                            while parent in self.parents:
                                parent = self.parents[parent]
                                self.folded.discard(parent)
                self.open_matches = False
            if self.reconnecting:
                # Results may have failed or become obsolete while disconnected.
                # Reload the selected reference after the recovered checkpoint.
                self.result_key = None
            self.reconnecting = False
            self._selection(self.rows())
            return True
        return False

    def _sort(self, row: Row) -> tuple:
        if row.placeholder:
            return (2, 0, 0, row.key)
        if self.sort in {"spend", "cost", "time"}:
            value = getattr(row.stats, "cost" if self.sort == "spend" else self.sort)
            return (value is None, -(value or 0), 0, row.key)
        active = (
            row.node.status in {"pending", "running"}
            if row.node and row.node.kind != "thread"
            else bool(row.node.active)
            if row.node
            else bool(self.snapshots[row.agent].active)
        )
        changed = (
            row.node.changed
            if row.node
            else max(
                (node.changed for node in self.snapshots[row.agent].threads), default=0
            )
        )
        return (0, not active, -changed, row.key)

    def _matches_agent(self, snapshot: ActivitySnapshot) -> bool:
        return (not self.display_query.active or bool(snapshot.active)) and (
            not self.display_query.text
            or self.display_query.text.casefold() in snapshot.agent.casefold()
            or bool(snapshot.matched or snapshot.threads)
        )

    def rows(self) -> list[Row]:
        groups: list[list[Row]] = []
        self.parents = {}
        for agent, snapshot in sorted(self.snapshots.items()):
            presence = "unknown" if self.reconnecting else snapshot.presence
            current = presence == "online" and not snapshot.stale
            summary = (
                counts(snapshot.active, snapshot.failed, snapshot.complete)
                if current
                else "-"
            )
            agent_match = (
                not self.display_query.text
                or self.display_query.text.casefold() in agent.casefold()
            )
            if self.view == "agent":
                if not self._matches_agent(snapshot):
                    continue
                groups.append([Row(agent, agent, None, snapshot.stats, summary)])
                continue
            if self.view == "thread":
                for node in snapshot.threads:
                    groups.append(
                        [
                            Row(
                                agent,
                                node.id,
                                node,
                                node.stats,
                                counts(
                                    node.active,
                                    node.failed,
                                    snapshot.complete
                                    and not snapshot.stale
                                    and not self.reconnecting,
                                )
                                if current
                                else "-",
                                thread=node.id,
                            )
                        ]
                    )
            else:
                by_parent: dict[str, list[ActivityNode]] = {}
                for node in snapshot.paths:
                    if node.parent:
                        by_parent.setdefault(node.parent, []).append(node)
                        self.parents[(agent, node.id)] = (agent, node.parent)
                for children in by_parent.values():
                    children.sort(
                        key=lambda n: (
                            n.position if n.position else [2**31],
                            n.created,
                            n.id,
                        )
                    )
                for root in snapshot.roots:
                    tree = (
                        self.tree
                        and root.status == "running"
                        and not root.stale
                        and not snapshot.stale
                        and current
                    )
                    root_row = Row(
                        agent,
                        root.id,
                        root,
                        root.stats,
                        node_summary(root),
                        root.id,
                        root.thread,
                        children=tree and bool(by_parent.get(root.id)),
                    )
                    rows = [root_row]
                    if tree:

                        def descend(parent: str, prefix: str, depth: int) -> None:
                            children = by_parent.get(parent, [])
                            for i, node in enumerate(children):
                                last = i == len(children) - 1
                                has_children = bool(by_parent.get(node.id))
                                folded = (agent, node.id) in self.folded
                                label = (
                                    prefix
                                    + ("└─ " if last else "├─ ")
                                    + node_summary(node)
                                )
                                if has_children and folded:
                                    label += " [+]"
                                rows.append(
                                    Row(
                                        agent,
                                        node.id,
                                        node,
                                        node.stats,
                                        label,
                                        root.id,
                                        root.thread,
                                        parent,
                                        depth,
                                        has_children,
                                    )
                                )
                                if has_children and not folded:
                                    descend(
                                        node.id,
                                        prefix + ("   " if last else "│  "),
                                        depth + 1,
                                    )

                        if root_row.key in self.folded:
                            if root_row.children:
                                root_row.activity += " [+]"
                        else:
                            descend(root.id, "", 1)
                    elif root.status == "running" and not root.stale:
                        leaves = []
                        pending = list(reversed(by_parent.get(root.id, [])))
                        while pending:
                            node = pending.pop()
                            children = by_parent.get(node.id, [])
                            if children:
                                pending.extend(reversed(children))
                            elif node.status == "running":
                                leaves.append(node)
                        if leaves and not snapshot.stale and not self.reconnecting:
                            root_row.activity = node_summary(leaves[0])
                            if len(leaves) > 1:
                                root_row.activity += f" · {len(leaves)} current calls"
                        elif snapshot.stale or self.reconnecting:
                            root_row.activity = f"stale · {root.title}"
                    if presence == "offline" or (
                        not current and root.status in {"pending", "running"}
                    ):
                        root_row.activity = "-"
                    groups.append(rows)
            if (
                not any(group[0].agent == agent for group in groups)
                and not self.agent
                and not self.display_query.active
                and agent_match
            ):
                groups.append(
                    [
                        Row(
                            agent,
                            agent,
                            None,
                            ActivityMetrics(
                                model=None, tool=None, cost=None, time=None
                            ),
                            summary,
                            placeholder=True,
                        )
                    ]
                )
        groups.sort(key=lambda group: self._sort(group[0]))
        return [row for group in groups for row in group]

    def _selection(self, rows: list[Row]) -> None:
        keys = {row.key for row in rows}
        candidate = self.selected
        while candidate and candidate not in keys:
            candidate = self.parents.get(candidate)
        if candidate not in keys:
            candidate = next(
                (key for key in self.selection_ancestors if key in keys), None
            )
        self.selected = (
            candidate if candidate in keys else rows[0].key if rows else None
        )
        if self.selected:
            index = next(i for i, row in enumerate(rows) if row.key == self.selected)
            self.offset = min(self.offset, index)
            self.offset = max(self.offset, index - self.page_size + 1)

    def _view(self, view: View) -> None:
        rows = self.rows()
        current = next((row for row in rows if row.key == self.selected), None)
        if current:
            if self.view == "execution":
                self.tree_selected = current.key
            ref = (
                current.agent
                if view == "agent"
                else current.thread
                if view == "thread"
                else current.root
            )
            self.selected = current.agent, ref
        self.view = view
        visible = self.rows()
        if current:
            candidates = [
                row
                for row in visible
                if row.agent == current.agent
                and (not current.thread or row.thread == current.thread)
            ]
            selected = next(
                (row for row in candidates if row.key == self.tree_selected), None
            )
            if selected and view == "execution" and self.tree:
                self.selected = selected.key
            elif self.selected not in {row.key for row in visible} and candidates:
                self.selected = candidates[0].key
        self._selection(visible)

    def key(self, key: str | Keys, data: str = "") -> bool:
        """Return True to exit; query edits set dirty for one subscription replacement."""
        if not self.editor and key in {Keys.ControlP, Keys.ControlN}:
            key = Keys.Up if key == Keys.ControlP else Keys.Down
        if key == Keys.ControlC:
            return True
        if self.editor:
            if key == Keys.BracketedPaste:
                self.buffer += clean(data)
            elif key == Keys.Escape:
                self.editor = None
                self.error = ""
            elif key == Keys.ControlU:
                self.buffer = ""
            elif key in (Keys.Backspace, Keys.ControlH):
                self.buffer = self.buffer[:-1]
            elif key == Keys.ControlA and self.editor == "filter":
                self.filter_active = not self.filter_active
            elif key == Keys.ControlM:
                try:
                    if self.editor == "filter":
                        self.query = replace(
                            self.query, text=self.buffer, active=self.filter_active
                        )
                        self.open_matches = True
                    self.editor = None
                    self.error = ""
                    self.dirty = True
                except ValueError as exc:
                    self.error = str(exc)
            elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                self.buffer += key
            return False
        if key == "q":
            return True
        if key == Keys.F1:
            self.help = not self.help
        elif key in {"a", "t", "e"}:
            self._view(cast(View, {"a": "agent", "t": "thread", "e": "execution"}[key]))
        elif key == Keys.F5 and self.view == "execution":
            current = next(
                (row for row in self.rows() if row.key == self.selected), None
            )
            self.tree = not self.tree
            if current and not self.tree:
                self.tree_selected = current.key
                self.selected = current.agent, current.root
            elif self.tree_selected:
                self.selected = self.tree_selected
        elif key == Keys.F6:
            self.sort = {"activity": "spend", "spend": "time", "time": "activity"}[
                self.sort
            ]
        elif key == Keys.F4:
            self.editor = "filter"
            self.buffer = self.query.text
            self.filter_active = self.query.active
        elif key in {Keys.F7, Keys.F8}:
            options = RECENT if key == Keys.F7 else SINCE
            value = self.recent_label if key == Keys.F7 else self.query.since
            value = (
                options[(options.index(value) + 1) % len(options)]
                if value in options
                else options[0]
            )
            if key == Keys.F7:
                self.query = replace(self.query, recent=duration(value))
                self.recent_label = value
            else:
                self.query = replace(self.query, since=value)
            self.dirty = True
        elif key == Keys.ControlM:
            self.details = not self.details
            self.details_offset = 0
        elif (self.details or self.help) and key in {Keys.PageUp, Keys.PageDown}:
            self.details_offset = max(
                0,
                self.details_offset
                + max(1, self.details_page_size - 1)
                * (1 if key == Keys.PageDown else -1),
            )
        elif key == Keys.Escape:
            self.details = False
            self.help = False
        elif key in {"<", ">"}:
            self.horizontal = max(0, self.horizontal + (16 if key == ">" else -16))
        else:
            rows = self.rows()
            self._selection(rows)
            index = next(
                (i for i, row in enumerate(rows) if row.key == self.selected), 0
            )
            if rows and key in {Keys.Up, Keys.Down, Keys.PageUp, Keys.PageDown}:
                change = (
                    -1
                    if key == Keys.Up
                    else 1
                    if key == Keys.Down
                    else -self.page_size
                    if key == Keys.PageUp
                    else self.page_size
                )
                self.selected = rows[max(0, min(len(rows) - 1, index + change))].key
            elif rows and key == Keys.Left:
                row = rows[index]
                if row.children and row.key not in self.folded:
                    self.folded.add(row.key)
                elif row.parent:
                    self.selected = row.agent, row.parent
            elif rows and key == Keys.Right:
                self.folded.discard(rows[index].key)
        self._selection(self.rows())
        return False

    def render(
        self, *, width: int = 140, height: int = 30, once: bool = False
    ) -> Group:
        from .activity_dashboard import render

        return render(self, width=width, height=height, once=once)
