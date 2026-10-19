"""Deterministic Top layout and owned-tree projection, without source reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from rich.cells import cell_len, set_cell_size
from rich.console import Console
from rich.style import Style
from rich.text import Text

from toolang.base.utils.tools import decode_tool_name, display_tool_name
from toolang.execution.operations import runtime_operation_name
from toolang.execution.schemas import LiveNode, RunNode, StepNode
from toolang.lang.types import parse_runnable_ref_parts
from toolang.stats.types import seconds
from toolang.up.schemas import SourceActivity, TopSnapshot

PERIODS = (3600, 14400, 28800)
RECENTS = (60, 300, 900)
SORT_FIELDS = ("MODEL", "TOOL", "INPUT", "CACHED", "OUTPUT", "SPEND", "TIME+")
Key = tuple[str, str, str]
TERMINAL = frozenset(("succeeded", "failed", "canceled"))
NORMAL = Style(bold=False, dim=False)
TABLE_HEADER = Style(color="black", bgcolor="green", bold=False, dim=False)
SELECTED = Style(bgcolor="#005f5f")
STATUS_BAR = Style(color="black", bgcolor="cyan", bold=False, dim=False)
FUNCTION_KEY = Style(color="white", bgcolor="black", bold=False, dim=False)


def window(value: int) -> str:
    return f"{value // 3600}h" if value >= 3600 else f"{value // 60}m"


def next_value(values: tuple[int, ...], current: int) -> int:
    return values[(values.index(current) + 1) % len(values)]


def runnable(value: str) -> str:
    try:
        parsed = parse_runnable_ref_parts(value)
    except ValueError:
        return value
    name = "" if parsed.role is not None else parsed.name
    return f"{parsed.kind}:{name}" if parsed.kind else name


def operation(node: LiveNode) -> str:
    if isinstance(node, RunNode):
        if node.status in TERMINAL:
            return node.status
        name = runnable(node.runnable)
        return f"pending · {name}" if node.status == "pending" else name
    if node.kind == "model":
        label = node.name
    elif node.kind == "tool":
        name = runtime_operation_name(node.name) if node.plugin == "runtime" else None
        if name in {"run", "spawn", "await", "exec"}:
            label = f"{'async run' if name == 'run' and node.asynchronous else name} {runnable(node.target)}".rstrip()
        else:
            decoded = decode_tool_name(node.name)
            label = display_tool_name(*decoded) if decoded else node.name
    else:
        name = (
            "async run"
            if node.operation == "run" and node.asynchronous
            else node.operation
        )
        label = f"{name} {runnable(node.target) or node.name}".rstrip()
    if node.status in TERMINAL:
        return f"{node.status} · {label}"
    if node.kind in {"model", "tool"} and not node.invoked:
        return f"preparing {label}"
    return label


def occurrence(node: LiveNode) -> str:
    value = node.occurrence
    if value is None:
        return ""
    labels = []
    for kind in ("item", "lane"):
        position = getattr(value, kind)
        if position is not None:
            labels.append(f"{kind} {position.index + 1}/{position.count}")
    if value.iteration is not None:
        labels.append(f"iteration {value.iteration.index + 1}")
    return " · ".join(labels)


def compact(value: int | None) -> str:
    if value is None:
        return "-"
    for divisor, suffix in ((1_000_000_000, "b"), (1_000_000, "m"), (1000, "k")):
        if value >= divisor:
            text = f"{value / divisor:.1f}".rstrip("0").rstrip(".") + suffix
            if len(text) <= 5:
                return text
            rounded = f"{value / divisor:.0f}{suffix}"
            return rounded if len(rounded) <= 5 else f"{value:.0e}".replace("e+", "e")
    return str(value)


def spend(value: float | None) -> str:
    return "-" if value is None else f"${value:.4f}"


def instance_name(source: SourceActivity) -> str:
    return source.label.partition("#")[0]


def elapsed(node: LiveNode, observed_at: str) -> int | None:
    if node.started_at is None:
        return None
    return max(0, seconds(node.finished_at or observed_at) - seconds(node.started_at))


def duration(node: LiveNode, observed_at: str) -> str:
    value = elapsed(node, observed_at)
    if value is None:
        return "-"
    if value >= 3600:
        return f"{value // 3600}h{value % 3600 // 60}m"
    if value >= 60:
        return f"{value // 60}m{value % 60}s"
    return f"{value}s"


def observed_at(source: SourceActivity, snapshot: TopSnapshot) -> str:
    return (
        source.live.observed_at
        if source.live_stale and source.live
        else snapshot.observed_at
    )


@dataclass(frozen=True)
class Row:
    key: Key
    root: str
    source: SourceActivity
    node: LiveNode
    parent: Key | None
    ancestors: tuple[Key, ...]
    branch: str
    status: str
    has_children: bool
    root_finished: bool


@dataclass
class TopView:
    scope: str = "too top"
    folded: set[Key] = field(default_factory=set)
    selected: Key | None = None
    list_mode: bool = False
    details: bool = False
    help: bool = False
    offset: int = 0
    rows: list[Row] = field(default_factory=list)
    page_size: int = 10
    sort_field: str | None = None
    sort_descending: bool = True
    sort_menu: bool = False
    sort_choice: int = 0
    sort_choice_descending: bool = True
    _activity: dict[Key, tuple[tuple[LiveNode, ...], tuple[int, int]]] = field(
        default_factory=dict
    )
    _revision: int = 0
    _row_hits: dict[int, Key] = field(default_factory=dict)
    _sort_hits: list[tuple[int, int, int, str]] = field(default_factory=list)

    def project(self, snapshot: TopSnapshot) -> list[Row]:
        roots: list[tuple[SourceActivity, RunNode, tuple[LiveNode, ...]]] = []
        current = snapshot.live_identities
        for source in snapshot.sources:
            active_roots = set()
            known_completed = (
                {node.id for node in source.completed.roots}
                if source.completed
                else set()
            )
            if source.live is not None and source.identity in current:
                for tree in source.live.trees:
                    root = next(
                        (n for n in tree.nodes if n.id == tree.root_run_id), None
                    )
                    if isinstance(root, RunNode):
                        if source.live_stale and root.id in known_completed:
                            continue
                        active_roots.add(root.id)
                        roots.append((source, root, tree.nodes))
            if source.completed is not None:
                cutoff = snapshot.recent
                for root in source.completed.roots:
                    if (
                        root.id not in active_roots
                        and root.finished_at is not None
                        and 0
                        <= seconds(snapshot.observed_at) - seconds(root.finished_at)
                        <= cutoff
                    ):
                        roots.append((source, root, (root,)))
        self._revision += 1
        activity = {}
        for source, root, nodes in roots:
            if root.status in TERMINAL:
                continue
            key = (source.identity, source.live.epoch if source.live else "", root.id)
            previous = self._activity.get(key)
            if previous is None:
                rank = (
                    max(
                        seconds(node.finished_at or node.started_at or node.created_at)
                        for node in nodes
                    ),
                    self._revision,
                )
            elif nodes != previous[0]:
                rank = (seconds(snapshot.observed_at), self._revision)
            else:
                rank = previous[1]
            activity[key] = (nodes, rank)
        self._activity = activity

        def default_order(item):
            source, node, _ = item
            finished = node.status in TERMINAL
            rank = (
                (seconds(node.finished_at or node.created_at), 0)
                if finished
                else activity[
                    (source.identity, source.live.epoch if source.live else "", node.id)
                ][1]
            )
            return finished, -rank[0], -rank[1], source.identity, node.id

        roots.sort(key=default_order)
        sort_field = self.sort_field
        if sort_field is not None:

            def metric(item):
                source, node, _ = item
                value = {
                    "MODEL": node.calls.model_calls,
                    "TOOL": node.calls.tool_calls,
                    "INPUT": node.usage.input_tokens if node.usage else None,
                    "CACHED": node.usage.cache_read_tokens if node.usage else None,
                    "OUTPUT": node.usage.output_tokens if node.usage else None,
                    "SPEND": node.cost,
                    "TIME+": elapsed(node, observed_at(source, snapshot)),
                }[sort_field]
                return (
                    node.status in TERMINAL,
                    value is None,
                    -(value or 0) if self.sort_descending else (value or 0),
                )

            roots.sort(key=metric)
        rows: list[Row] = []
        all_parents: dict[Key, Key | None] = {}
        keys: set[Key] = set()
        for source, root, nodes in roots:
            epoch = source.live.epoch if source.live else ""
            children: dict[str, list[LiveNode]] = {}
            by_id = {node.id: node for node in nodes}
            for node in nodes:
                key = (source.identity, epoch, node.id)
                keys.add(key)
                all_parents[key] = (
                    (source.identity, epoch, node.parent) if node.parent else None
                )
                if node.parent is not None:
                    children.setdefault(node.parent, []).append(node)
            # LiveTree already supplies admission order; IDs do not order lanes.
            unfinished: dict[str, bool] = {}

            def active(node: LiveNode) -> bool:
                if node.id not in unfinished:
                    unfinished[node.id] = node.status not in TERMINAL or any(
                        active(child) for child in children.get(node.id, ())
                    )
                return unfinished[node.id]

            def folded_label(node: LiveNode) -> str:
                label = operation(node)
                if isinstance(node, RunNode) and node.status in TERMINAL:
                    return label
                current = node
                branch_count = 0
                while True:
                    paths = [
                        child for child in children.get(current.id, ()) if active(child)
                    ]
                    if (isinstance(current, StepNode) and current.kind == "par") or len(
                        paths
                    ) > 1:
                        branch_count = len(paths)
                        break
                    if len(paths) != 1:
                        break
                    if isinstance(node, StepNode) and node.status in TERMINAL:
                        branch_count = len(paths)
                        break
                    current = paths[0]
                if current.id != node.id:
                    projected = operation(current)
                    label = (
                        f"{label} · {projected}"
                        if isinstance(node, RunNode)
                        else projected
                    )
                if branch_count:
                    label += f" · {branch_count} active"
                return label

            def visit(
                node: LiveNode, ancestors: tuple[Key, ...], guides: str, branch: str
            ) -> None:
                key = (source.identity, epoch, node.id)
                group = children.get(node.id, [])
                folded = bool(group) and (self.list_mode or key in self.folded)
                label = folded_label(node) if folded else operation(node)
                position = occurrence(node)
                if position:
                    label += " · " + position
                if (
                    node.id == root.id
                    and source.live_stale
                    and node.status not in TERMINAL
                ):
                    label += " · stale"
                rows.append(
                    Row(
                        key,
                        root.id,
                        source,
                        node,
                        all_parents[key],
                        ancestors,
                        branch,
                        label,
                        bool(group),
                        root.status in TERMINAL,
                    )
                )
                if not folded:
                    for index, child in enumerate(group):
                        last = index == len(group) - 1
                        visit(
                            child,
                            (*ancestors, key),
                            guides + ("   " if last else "│  "),
                            guides + ("└─ " if last else "├─ "),
                        )

            visit(by_id[root.id], (), "", "")
        self.folded.intersection_update(keys)
        visible = {row.key for row in rows}
        if self.selected not in visible:
            previous = next(
                (row for row in self.rows if row.key == self.selected), None
            )
            waiting = next(
                (
                    source
                    for source in snapshot.sources
                    if self.selected is not None
                    and source.identity == self.selected[0]
                    and source.completed is None
                    and source.activity_error is None
                ),
                None,
            )
            if (
                waiting is not None
                and self.selected is not None
                and self.selected not in all_parents
            ):
                # Keep selection through a completion/window handoff; no row or
                # total is fabricated while its new Recent query is loading.
                self.selected = (
                    waiting.identity,
                    waiting.live.epoch if waiting.live else self.selected[1],
                    previous.root if previous else self.selected[2],
                )
            else:
                selected = self.selected
                while selected is not None and selected not in visible:
                    selected = all_parents.get(selected)
                if selected is None and previous is not None:
                    selected = next(
                        (
                            row.key
                            for row in rows
                            if row.source.identity == previous.source.identity
                            and row.node.id == previous.root
                        ),
                        None,
                    )
                self.selected = selected or (rows[0].key if rows else None)
        self.rows = rows
        return rows

    def move(self, delta: int) -> None:
        if self.sort_menu:
            choice = max(0, min(len(SORT_FIELDS), self.sort_choice + delta))
            if choice != self.sort_choice:
                self.sort_choice = choice
                self.sort_choice_descending = True
            return
        if self.rows:
            index = next(
                (i for i, row in enumerate(self.rows) if row.key == self.selected), 0
            )
            self.selected = self.rows[
                max(0, min(len(self.rows) - 1, index + delta))
            ].key

    def left(self) -> None:
        if self.sort_menu:
            self.sort_choice_descending = False
            return
        row = next((row for row in self.rows if row.key == self.selected), None)
        if row is not None:
            if row.has_children and row.key not in self.folded and not self.list_mode:
                self.folded.add(row.key)
            elif row.parent is not None:
                self.selected = row.parent

    def right(self) -> None:
        if self.sort_menu:
            self.sort_choice_descending = True
            return
        row = next((row for row in self.rows if row.key == self.selected), None)
        if row is not None:
            if row.key in self.folded and not self.list_mode:
                self.folded.remove(row.key)
            elif not self.list_mode:
                child = next(
                    (item for item in self.rows if item.parent == row.key), None
                )
                if child is not None:
                    self.selected = child.key

    def sort(self) -> None:
        if self.sort_menu:
            self.sort_field = (
                SORT_FIELDS[self.sort_choice - 1] if self.sort_choice else None
            )
            self.sort_descending = self.sort_choice_descending
        else:
            self.sort_choice = (
                SORT_FIELDS.index(self.sort_field) + 1 if self.sort_field else 0
            )
            self.sort_choice_descending = self.sort_descending
        self.sort_menu = not self.sort_menu
        self.details = self.help = False

    def click(self, x: int, y: int) -> None:
        if y in self._row_hits:
            self.selected = self._row_hits[y]
        for row, start, end, name in self._sort_hits:
            if y == row and start <= x < end:
                self.sort_descending = (
                    not self.sort_descending if self.sort_field == name else True
                )
                self.sort_field = name
                self.sort_menu = False
                break

    def footer(self, snapshot: TopSnapshot, width: int | None = None) -> Text:
        labels = (
            f"F1 Help   F3 {'Close' if self.details else 'Details'}   "
            f"F5 {'Tree' if self.list_mode else 'List'}   "
            f"F6 {'Apply' if self.sort_menu else 'Sort'}   "
            f"F7 Runs({window(next_value(RECENTS, snapshot.recent))})   "
            f"F8 Stats({window(next_value(PERIODS, snapshot.period))})   F10 Quit"
        )
        if width is not None and cell_len(labels) > width:
            labels = " ".join(labels.split())
            if cell_len(labels) > width:
                labels = "F1 Help F10 Quit"
        text = Text(labels, style=STATUS_BAR)
        text.highlight_regex(r"\bF\d+\b", FUNCTION_KEY)
        return text

    def notices(self, snapshot: TopSnapshot) -> list[str]:
        notices = []
        if snapshot.inventory.diagnostics:
            notices.append("agents unavailable")
        elif not snapshot.inventory_updating and snapshot.summary.running is None:
            notices.append("agents unavailable")
        if any(source.activity_error for source in snapshot.sources):
            notices.append("runs unavailable")
        elif any(
            source.live_error and source.identity in snapshot.live_identities
            for source in snapshot.sources
        ):
            notices.append("live unavailable")
        if any(source.usage_error for source in snapshot.sources):
            notices.append("stats unavailable")
        return notices

    def header(self, snapshot: TopSnapshot, width: int) -> list[Text]:
        clock = datetime.fromtimestamp(seconds(snapshot.observed_at)).strftime(
            "%H:%M:%S"
        )
        scope = Text(self.scope, style=NORMAL)
        scope.truncate(max(0, width - 10), overflow="ellipsis")
        title = scope.copy()
        notices = self.notices(snapshot)
        budget = max(0, width - scope.cell_len - 8 - 4)
        if notices and budget:
            notice = Text(" · ".join(notices), style="yellow")
            if notice.cell_len > budget:
                suffix = f" +{len(notices) - 1}" if len(notices) > 1 else ""
                notice = Text(notices[0], style="yellow")
                notice.truncate(max(0, budget - len(suffix)), overflow="ellipsis")
                notice.append(suffix)
                notice.truncate(budget, overflow="ellipsis")
            title.append("  ")
            title.append_text(notice)
        title.append(" " * max(0, width - title.cell_len - 8))
        title.append(clock)
        lines = [title, Text()]
        summary = snapshot.summary

        def metric(value: str, label: str) -> Text:
            text = Text(style=NORMAL)
            text.append(
                value, style="cyan" if value not in ("-", "0", "$0.0000") else NORMAL
            )
            text.append(" " + label, style=NORMAL)
            return text

        def count(value: int | None) -> str:
            return "-" if value is None else str(value)

        placements = Text(", ", style=NORMAL).join(
            metric(count(value), label)
            for value, label in (
                (summary.resident, "resident"),
                (summary.visiting, "visiting"),
                (summary.roaming, "roaming"),
            )
        )
        agents = Text.assemble(
            metric(count(summary.running), "running"),
            ", ",
            metric(count(summary.total), "total"),
            ": ",
            placements,
            style=NORMAL,
        )
        terminal = summary.completed
        runs = Text.assemble(
            metric(count(summary.active), "active"),
            ", ",
            metric(str(terminal.total) if terminal else "-", "completed"),
            ": ",
            style=NORMAL,
        )
        runs.append_text(
            Text(", ", style=NORMAL).join(
                metric(str(getattr(terminal, state)) if terminal else "-", state)
                for state in ("succeeded", "failed", "canceled")
            )
        )
        total = summary.usage
        stats = Text(", ", style=NORMAL).join(
            metric(value, label)
            for value, label in (
                (compact(total.model_calls) if total else "-", "model calls"),
                (compact(total.tool_calls) if total else "-", "tool calls"),
                (compact(total.input_tokens) if total else "-", "input"),
                (compact(total.cached_tokens) if total else "-", "cached"),
                (compact(total.output_tokens) if total else "-", "output"),
                (
                    spend(
                        total.spend_micros / 1e6
                        if total.spend_micros is not None
                        else None
                    )
                    if total
                    else "-",
                    "spend",
                ),
            )
        )
        inner_width = max(1, width - 4)
        caption_width = 15 if inner_width >= 37 else 0
        left_width = max(1, inner_width - caption_width - (2 if caption_width else 0))
        console = Console(width=left_width)
        for text, caption in (
            (agents, "agents"),
            (runs, f"runs last {window(snapshot.recent)}"),
            (stats, f"stats last {window(snapshot.period)}"),
        ):
            for index, part in enumerate(text.wrap(console, left_width)):
                line = Text("  ", style=NORMAL)
                line.append_text(part)
                line.pad_right(2 + left_width - line.cell_len)
                if caption_width:
                    line.append("  ")
                    line.append(
                        caption.rjust(caption_width)
                        if index == 0
                        else " " * caption_width,
                        style=Style(dim=True),
                    )
                line.append("  ")
                lines.append(line)
        return [*lines, Text()]

    def render(
        self, snapshot: TopSnapshot, *, width: int, height: int | None = None
    ) -> Text:
        self.project(snapshot)
        lines = self.header(snapshot, width)
        agent_width = max(
            [5, *(cell_len(instance_name(row.source)) for row in self.rows)]
        )
        run_width = max([3, *(cell_len(row.root) for row in self.rows)])
        spend_width = max([9, *(len(spend(row.node.cost)) for row in self.rows)])
        widths = [agent_width, run_width, 5, 5, 6, 6, 6, spend_width, 7]
        headings = [
            "AGENT",
            "RUN",
            "MODEL",
            "TOOL",
            "INPUT",
            "CACHED",
            "OUTPUT",
            "SPEND",
            "TIME+",
        ]
        for index, name in enumerate(headings):
            if name == self.sort_field:
                headings[index] += "↓" if self.sort_descending else "↑"
                widths[index] = max(widths[index], cell_len(headings[index]))
        self._row_hits.clear()
        self._sort_hits.clear()
        column = 0
        for name, size in zip(("AGENT", "RUN", *SORT_FIELDS), widths, strict=True):
            if name in SORT_FIELDS:
                self._sort_hits.append((len(lines), column, column + size, name))
            column += size + 1

        def prefix(values: list[str] | tuple[str, ...]) -> str:
            return (
                " ".join(
                    set_cell_size(value, size) if index < 2 else value.rjust(size)
                    for index, (value, size) in enumerate(zip(values, widths))
                )
                + " "
            )

        lines.append(Text(prefix(headings) + "STATUS", style=TABLE_HEADER))
        panel = []
        if self.help:
            panel = [
                Text(value)
                for value in (
                    "Help — F1 or Esc closes",
                    "↑/↓ select · ← fold/parent · → expand/child · PgUp/PgDn page",
                    "F3/Enter details · F5 Tree/List · F6 sort · F7 recent completions · F8 usage period",
                    "Click a row to select; click a numeric heading to sort or reverse its direction.",
                    "F10, q or Ctrl-C quit. Rows show lifetime consumption; parents include children.",
                    "Window calls settle at Step completion and belong to Step start; usage belongs to finish.",
                    "Unfinished calls appear in live rows. Stale live data freezes; '-' means unknown.",
                )
            ]
        elif self.details:
            panel = [
                Text("Details", style="bold"),
                Text(
                    "Details are not defined yet. F3, Enter or Esc closes this placeholder."
                ),
            ]
        elif self.sort_menu:
            panel = [
                Text(
                    "Sort — ↑/↓ field · ← ascending · → descending · F6/Enter apply · Esc cancel"
                ),
                Text(
                    "  ".join(
                        f"[{name}]" if index == self.sort_choice else name
                        for index, name in enumerate(("Default", *SORT_FIELDS))
                    )
                ),
                Text(
                    "Order: "
                    + ("descending" if self.sort_choice_descending else "ascending")
                ),
            ]
        if height is not None:
            panel = panel[: max(1, (height - len(lines) - 1) // 2)]
            self.page_size = max(1, height - len(lines) - len(panel) - 1)
            index = next(
                (i for i, row in enumerate(self.rows) if row.key == self.selected), 0
            )
            self.offset = min(
                self.offset, index, max(0, len(self.rows) - self.page_size)
            )
            self.offset = max(self.offset, index - self.page_size + 1)
            displayed = self.rows[self.offset : self.offset + self.page_size]
        else:
            displayed = self.rows
        for row in displayed:
            self._row_hits[len(lines)] = row.key
            node = row.node
            calls = (
                [compact(node.calls.model_calls), compact(node.calls.tool_calls)]
                if isinstance(node, RunNode)
                else [
                    str(int(node.invoked and node.kind == kind))
                    for kind in ("model", "tool")
                ]
                if node.kind in {"model", "tool"}
                else ["-", "-"]
            )
            usage = node.usage
            values = [
                instance_name(row.source),
                row.root,
                *calls,
                compact(usage.input_tokens if usage else None),
                compact(usage.cache_read_tokens if usage else None),
                compact(usage.output_tokens if usage else None),
                spend(node.cost),
                duration(node, observed_at(row.source, snapshot)),
            ]
            start = prefix(values)
            marker = "+ " if node.id == row.root and not row.root_finished else "  "
            label = Text(
                (marker + row.branch + row.status)
                .replace("\n", " ")
                .replace("\r", " ")
                .replace("\t", " ")
            )
            label.truncate(max(0, width - cell_len(start)), overflow="ellipsis")
            line = Text(start, style=Style(bold=False, dim=row.root_finished))
            line.append_text(label)
            line.pad_right(max(0, width - line.cell_len))
            if height is not None and row.key == self.selected:
                line.stylize(SELECTED)
            lines.append(line)
        if height is not None:
            while len(lines) < height - len(panel) - 1:
                lines.append(Text())
        lines.extend(panel)
        lines.append(self.footer(snapshot, width))
        for line in lines:
            line.truncate(width, overflow="ellipsis")
            line.pad_right(max(0, width - line.cell_len))
        return Text("\n").join(lines)
