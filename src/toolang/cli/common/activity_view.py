"""Top views, row ownership, and terminal interaction; no execution or accounting."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
import time
from typing import Literal, cast

from prompt_toolkit.keys import Keys
from rich.console import Console, Group
from rich.cells import cell_len
from rich.text import Text

from toolang.execution.activity import ActivityQuery
from toolang.execution.schemas import ActivityMetrics, ActivityNode, ActivitySnapshot

View = Literal["agent", "thread", "execution"]
Sort = Literal["activity", "cost", "time"]
RECENT = ("5m", "30m", "1h", "1d", "1w", "all")
SINCE = ("session", "1h", "1d", "1w", "all")


def duration(value: str) -> float | None:
    if value == "all":
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhdw])", value)
    if not match or float(match[1]) <= 0:
        raise ValueError("Use a positive duration such as 30m, 1d, 1w, or all")
    return (
        float(match[1])
        * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
    )


def since(value: str, *, now: float | None = None) -> str:
    if value in {"session", "all"}:
        return value
    if re.fullmatch(r"\d+(?:\.\d+)?[smhdw]", value):
        seconds = duration(value)
        assert seconds is not None
        return datetime.fromtimestamp(
            (now if now is not None else time.time()) - seconds, timezone.utc
        ).isoformat()
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Stats timestamp must include a timezone")
    return stamp.isoformat()


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
    return (
        ("~" if metrics.estimated else "")
        + f"${metrics.cost:.2f}"
        + ("+" if metrics.partial or not metrics.complete else "")
    )


def counts(active: int, failed: int, complete: bool = True) -> str:
    values = [f"{active} active"] if active else []
    if failed:
        values.append(f"{failed} failed")
    if not complete:
        values.append("counts incomplete")
    return " · ".join(values) or "idle"


def clean(value: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", value)


def node_summary(node: ActivityNode) -> str:
    label = node.title + (f" · {node.summary}" if node.summary else "")
    if node.stale:
        return f"stale · {label}"
    if node.status not in {"running", "idle"}:
        return f"{node.status} · {node.summary or node.title}"
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
    ) -> None:
        self.agent = agent
        self.view: View = view or ("thread" if agent else "agent")
        self.tree = tree
        self.sort = sort
        self.query = query or ActivityQuery()
        self.recent_label = recent_label
        self.display_query = self.query
        self.display_recent = recent_label
        self.attached_query = self.query
        self.attached_recent = recent_label
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
        self.editor: str | None = None
        self.buffer = ""
        self.error = ""
        self.page_size = 15
        self.width = 140
        self.dirty = False

    def attach(self) -> None:
        self.pending.clear()
        self.attached_query = self.query
        self.attached_recent = self.recent_label

    def feed(self, event: str, data: dict) -> bool:
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
            self.snapshots, self.pending = self.pending, {}
            self.ready = True
            self.display_query = self.attached_query
            self.display_recent = self.attached_recent
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
            self.reconnecting = False
            self._selection(self.rows())
            return True
        return False

    def _sort(self, row: Row) -> tuple:
        if row.placeholder:
            return (2, 0, 0, row.key)
        if self.sort in {"cost", "time"}:
            value = getattr(row.stats, self.sort)
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
            marker = ""
            presence = "unknown" if self.reconnecting else snapshot.presence
            if presence != "online":
                marker = f"{presence} · last seen {elapsed(time.time() - snapshot.observed)} ago · "
            if snapshot.stale and snapshot.presence == "online":
                marker += "stale · "
            if not snapshot.complete:
                marker += "syncing · "
            summary = marker + counts(
                snapshot.active, snapshot.failed, snapshot.complete
            )
            agent_match = (
                not self.display_query.text
                or self.display_query.text.casefold() in agent.casefold()
            )
            if self.view == "agent":
                if self.agent or not self._matches_agent(snapshot):
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
                                marker
                                + counts(
                                    node.active,
                                    node.failed,
                                    snapshot.complete
                                    and not snapshot.stale
                                    and not self.reconnecting,
                                ),
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
                        and not self.reconnecting
                    )
                    root_row = Row(
                        agent,
                        root.id,
                        root,
                        root.stats,
                        node_summary(root),
                        root.id,
                        root.thread,
                        children=bool(by_parent.get(root.id)),
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

    def key(self, key: str | Keys) -> bool:
        """Return True to exit; query edits set dirty for one subscription replacement."""
        if key == Keys.ControlC:
            return True
        if self.editor:
            if key == Keys.Escape:
                self.editor = None
                self.error = ""
            elif key == Keys.ControlU:
                self.buffer = ""
            elif key in (Keys.Backspace, Keys.ControlH):
                self.buffer = self.buffer[:-1]
            elif key == Keys.ControlA and self.editor == "filter":
                self.query = replace(self.query, active=not self.query.active)
                self.dirty = True
            elif key == Keys.Tab and self.editor in {"recent", "since"}:
                options = RECENT if self.editor == "recent" else SINCE
                self.buffer = (
                    options[(options.index(self.buffer) + 1) % len(options)]
                    if self.buffer in options
                    else options[0]
                )
            elif key == Keys.ControlM:
                try:
                    if self.editor == "filter":
                        self.query = replace(self.query, text=self.buffer)
                        self.open_matches = True
                    elif self.editor == "recent":
                        self.query = replace(self.query, recent=duration(self.buffer))
                        self.recent_label = self.buffer
                    else:
                        self.query = replace(self.query, since=since(self.buffer))
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
        if key in {"a", "t", "e"}:
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
            self.sort = {"activity": "cost", "cost": "time", "time": "activity"}[
                self.sort
            ]
        elif key in {Keys.F4, Keys.F7, Keys.F8}:
            self.editor = {Keys.F4: "filter", Keys.F7: "recent", Keys.F8: "since"}[
                Keys(key)
            ]
            self.buffer = (
                self.query.text
                if self.editor == "filter"
                else self.recent_label
                if self.editor == "recent"
                else self.query.since
            )
        elif key == Keys.ControlM:
            self.details = not self.details
        elif key == Keys.Escape:
            self.details = False
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
        self.width = width
        if not self.ready:
            return Group(
                Text("too top · Connecting to activity service"),
                Text("q Quit", style="dim"),
            )
        time_label = "TIME" if self.display_query.since == "session" else "TIME*"
        rows = self.rows()
        self._selection(rows)
        values: list[Text] = []
        stats_label = (
            self.display_query.since
            if self.display_query.since != "all"
            else "all available history"
        )
        values.append(
            Text(
                f"too top  View {self.view.title()}"
                + (
                    f"  Layout {'Tree' if self.tree else 'List'}"
                    if self.view == "execution"
                    else ""
                )
                + f"  Stats {stats_label}  Recent {self.display_recent}  Sort {self.sort}",
                style="bold",
            )
        )
        snapshots = list(self.snapshots.values())
        if self.agent:
            snapshot = self.snapshots.get(self.agent)
            if snapshot:
                presence = "unknown" if self.reconnecting else snapshot.presence
                values.append(
                    Text(
                        f"{self.agent} · {presence}  Agent Stats: MODEL {snapshot.stats.model if snapshot.stats.model is not None else '-'}  TOOL {snapshot.stats.tool if snapshot.stats.tool is not None else '-'}  COST {cost(snapshot.stats)}  {time_label} {elapsed(snapshot.stats.time)}  {snapshot.thread_count} threads"
                    )
                )
        else:
            known = [snapshot.stats for snapshot in snapshots]
            total = ActivityMetrics(
                model=sum(item.model or 0 for item in known)
                if any(item.model is not None for item in known)
                else None,
                tool=sum(item.tool or 0 for item in known)
                if any(item.tool is not None for item in known)
                else None,
                cost=sum(item.cost or 0 for item in known)
                if any(item.cost is not None for item in known)
                else None,
                time=sum(item.time or 0 for item in known)
                if any(item.time is not None for item in known)
                else None,
                estimated=any(item.estimated for item in known),
                partial=any(item.cost is None or item.partial for item in known),
                complete=all(item.complete for item in known),
            )
            values.append(
                Text(
                    f"Agents {sum(s.presence == 'online' for s in snapshots) if not self.reconnecting else '?'} online / {len(snapshots)}  Agent Stats: MODEL {total.model if total.model is not None else '-'}  TOOL {total.tool if total.tool is not None else '-'}  COST {cost(total)}  {time_label} {elapsed(total.time)}"
                )
            )
        if self.view == "agent":
            label = "Agents"
            matched = sum(self._matches_agent(s) for s in snapshots)
            eligible = len(snapshots)
            loaded = matched
        elif self.view == "thread":
            label = "Threads"
            matched = sum(s.thread_matched for s in snapshots)
            eligible = sum(s.thread_eligible for s in snapshots)
            loaded = sum(len(s.threads) for s in snapshots)
        else:
            label = "Root runs"
            matched = sum(s.matched for s in snapshots)
            eligible = sum(s.eligible for s in snapshots)
            loaded = sum(len(s.roots) for s in snapshots)
        values.append(
            Text(
                f"{label} {matched}/{eligible} matched/eligible  Loaded {loaded}/{matched}"
                + f"  Root runs {sum(s.active for s in snapshots)} active · {sum(s.failed for s in snapshots)} failed"
                + f"  Total threads {sum(s.thread_count for s in snapshots)}"
                + ("  Reconnecting · presence unknown" if self.reconnecting else "")
            )
        )
        if self.display_query.since == "session":
            starts = [
                datetime.fromtimestamp(s.session_start, timezone.utc).isoformat(
                    timespec="seconds"
                )
                for s in snapshots
                if s.session_start
            ]
            uptime = ""
            if len(snapshots) == 1 and snapshots[0].session_start is not None:
                uptime = f" · Uptime {elapsed(snapshots[0].observed - snapshots[0].session_start)}"
            values.append(
                Text(
                    "Stats: each executor session"
                    + (f" from {starts[0]}" if len(starts) == 1 else "")
                    + uptime
                    + " · TIME sums root duration",
                    style="dim",
                )
            )
        else:
            values.append(
                Text(
                    "TIME*: MODEL, TOOL, COST and TIME use the same selected Stats range",
                    style="dim",
                )
            )
        for snapshot in snapshots:
            if snapshot.coverage or not snapshot.stats.complete:
                values.append(
                    Text(
                        f"{snapshot.agent}: {snapshot.coverage or 'Stats coverage incomplete'}",
                        style="yellow",
                    )
                )

        def column_width(field: str) -> int:
            return max(
                8,
                max(
                    (
                        cell_len(clean(str(getattr(row, field)).removeprefix("agent:")))
                        for row in rows
                    ),
                    default=0,
                ),
            )

        columns = [] if self.agent else [("AGENT", column_width("agent"))]
        if width >= 110:
            columns += [("MODEL", 5), ("TOOL", 5)]
        columns += [("COST", 10), (time_label, 8)]
        if self.view != "agent":
            columns.append(("THREAD", column_width("thread")))
        if self.view == "execution":
            columns += [("RUN", column_width("root")), ("STEP", column_width("id"))]
        show_table = not (self.agent and self.view == "agent")
        if show_table:
            heading = (
                " ".join(label.ljust(size) for label, size in columns) + " ACTIVITY"
            )
            values.append(
                Text(
                    heading[self.horizontal : self.horizontal + width],
                    style="bold reverse",
                )
            )

        def render_row(row: Row) -> Text:
            fields = {
                "AGENT": row.agent.removeprefix("agent:"),
                "MODEL": str(row.stats.model) if row.stats.model is not None else "-",
                "TOOL": str(row.stats.tool) if row.stats.tool is not None else "-",
                "COST": cost(row.stats),
                "TIME": elapsed(row.stats.time),
                "TIME*": elapsed(row.stats.time),
                "THREAD": row.thread or "-",
                "RUN": row.root or "-",
                "STEP": row.id if row.root else "-",
            }
            line = (
                " ".join(
                    (
                        " " * max(0, size - cell_len(fields[label])) + fields[label]
                        if label in {"MODEL", "TOOL", "COST", "TIME", "TIME*"}
                        else fields[label]
                        + " " * max(0, size - cell_len(fields[label]))
                    )
                    for label, size in columns
                )
                + " "
                + row.activity
            )
            rendered = Text(
                clean(line)[self.horizontal : self.horizontal + width],
                style="reverse" if not once and row.key == self.selected else "",
                no_wrap=True,
                overflow="crop",
            )
            if self.display_query.text:
                rendered.highlight_words(
                    [self.display_query.text],
                    style="bold yellow",
                    case_sensitive=False,
                )
            return rendered

        footer: list[Text] = []
        current = next((row for row in rows if row.key == self.selected), None)
        if current and (self.details or once is False):
            command = (
                f"too {current.agent.removeprefix('agent:')} inspect {current.id}"
                if current.node and current.node.kind != "thread"
                else ""
            )
            footer.append(
                Text(
                    f"Selected: {current.agent} / {current.thread or '-'} / root {current.root or '-'} / {current.id}"
                    + (f"  Inspect: {command}" if command else ""),
                    overflow="fold",
                )
            )
            if self.details:
                footer.append(Text(clean(current.activity), overflow="fold"))
                footer.append(
                    Text(
                        f"Stats: MODEL {current.stats.model} TOOL {current.stats.tool} COST {cost(current.stats)} TIME {elapsed(current.stats.time)}"
                    )
                )
                if current.node:
                    total = current.node.total
                    footer.append(
                        Text(
                            f"Total: MODEL {total.model} TOOL {total.tool} COST {cost(total)} TIME {elapsed(total.time)}"
                        )
                    )
                    if current.node.matches:
                        footer.append(
                            Text(
                                "Matched IDs: " + ", ".join(current.node.matches),
                                overflow="fold",
                            )
                        )
        if not once:
            if self.editor:
                footer.append(
                    Text(
                        f"{self.editor.title()}: {self.buffer}█  Enter apply · Esc cancel · Ctrl-U clear · Tab presets · Ctrl-A active={self.display_query.active}",
                        style="bold",
                    )
                )
                if self.error:
                    footer.append(Text(self.error, style="red"))
            else:
                footer.append(
                    Text(
                        "a Agent  t Thread  e Execution  F4 Filter  F5 Layout  F6 Sort  F7 Recent  F8 Stats  Enter Details  <> Scroll  q Quit",
                        style="dim",
                    )
                )
        if once:
            body = [render_row(row) for row in rows] if show_table else []
            if show_table and not rows:
                body.append(Text("No matching activity", style="dim"))
            return Group(*values, *body, *footer)

        console = Console(width=width)
        header_lines = [line for value in values for line in value.wrap(console, width)]
        footer_lines = [line for value in footer for line in value.wrap(console, width)]
        # Reserve space for the selected row and controls even with long IDs,
        # details, coverage warnings, or a narrow terminal.
        footer_limit = max(1, height // 2)
        if len(footer_lines) > footer_limit:
            footer_lines = [*footer_lines[: footer_limit - 1], footer_lines[-1]]
        header_limit = max(0, height - len(footer_lines) - int(show_table))
        if len(header_lines) > header_limit:
            header_lines = (
                [*header_lines[: header_limit - 1], header_lines[-1]]
                if header_limit
                else []
            )
        self.page_size = max(1, height - len(header_lines) - len(footer_lines))
        self._selection(rows)
        body = (
            [
                render_row(row)
                for row in rows[self.offset : self.offset + self.page_size]
            ]
            if show_table
            else []
        )
        if show_table and not rows:
            body.append(Text("No matching activity", style="dim"))
        return Group(*header_lines, *body, *footer_lines)
