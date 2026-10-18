"""Deterministic Top layout and owned-tree projection, without source reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from rich.cells import cell_len, set_cell_size
from rich.console import Console
from rich.text import Text

from toolang.base.utils.tools import decode_tool_name, display_tool_name
from toolang.execution.operations import runtime_operation_name
from toolang.execution.schemas import LiveNode, RunNode, StepNode
from toolang.lang.types import parse_runnable_ref_parts
from toolang.stats.types import seconds
from toolang.up.schemas import SourceActivity, TopSnapshot

PERIODS = (3600, 14400, 28800)
RECENTS = (60, 300, 900)
Key = tuple[str, str, str]
TERMINAL = frozenset(("succeeded", "failed", "canceled"))


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
            return f"{value / divisor:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def spend(value: float | None, approximate: bool = False) -> str:
    return "-" if value is None else f"{'~' if approximate else ''}${value:.2f}"


def duration(node: LiveNode, observed_at: str) -> str:
    if node.started_at is None:
        return "-"
    elapsed = max(
        0, seconds(node.finished_at or observed_at) - seconds(node.started_at)
    )
    if elapsed >= 3600:
        return f"{elapsed // 3600}h{elapsed % 3600 // 60}m"
    if elapsed >= 60:
        return f"{elapsed // 60}m{elapsed % 60}s"
    return f"{elapsed}s"


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

    def project(self, snapshot: TopSnapshot) -> list[Row]:
        roots: list[tuple[SourceActivity, RunNode, tuple[LiveNode, ...]]] = []
        for source in snapshot.sources:
            active_roots = set()
            known_completed = (
                {node.id for node in source.completed.roots}
                if source.completed
                else set()
            )
            if source.live is not None:
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
        roots.sort(
            key=lambda item: (
                item[1].status in TERMINAL,
                -seconds(item[1].finished_at or item[1].created_at)
                if item[1].status in TERMINAL
                else seconds(item[1].created_at),
                item[0].identity,
                item[1].id,
            )
        )
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
                if folded:
                    label += " [+]"
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
        if self.rows:
            index = next(
                (i for i, row in enumerate(self.rows) if row.key == self.selected), 0
            )
            self.selected = self.rows[
                max(0, min(len(self.rows) - 1, index + delta))
            ].key

    def left(self) -> None:
        row = next((row for row in self.rows if row.key == self.selected), None)
        if row is not None:
            if row.has_children and row.key not in self.folded and not self.list_mode:
                self.folded.add(row.key)
            elif row.parent is not None:
                self.selected = row.parent

    def right(self) -> None:
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

    def footer(self, snapshot: TopSnapshot) -> str:
        return (
            f"F1 Help   F3 {'Close' if self.details else 'Details'}   "
            f"F5 {'Tree' if self.list_mode else 'List'}   "
            f"F7 Runs({window(next_value(RECENTS, snapshot.recent))})   "
            f"F8 Stats({window(next_value(PERIODS, snapshot.period))})   F10 Quit"
        )

    def header(self, snapshot: TopSnapshot, width: int) -> list[Text]:
        clock = datetime.fromtimestamp(seconds(snapshot.observed_at)).strftime(
            "%H:%M:%S"
        )
        lines = [
            Text(self.scope + " " * max(1, width - cell_len(self.scope) - 8) + clock),
            Text(),
        ]
        summary = snapshot.summary
        counts = (summary.resident, summary.visiting, summary.roaming)
        unknown_inventory = snapshot.inventory_updating or snapshot.inventory.partial
        agents = ", ".join(
            f"{'-' if count is None else count} {kind}"
            for count, kind in zip(counts, ("resident", "visiting", "roaming"))
        )
        active_unknown = summary.active is None
        completed_unknown = summary.completed is None
        terminal = summary.completed
        completed_total = terminal.total if terminal else "-"
        runs = f"{'-' if active_unknown else summary.active} active, {completed_total} completed: "
        runs += ", ".join(
            f"{getattr(terminal, state) if terminal else '-'} {state}"
            for state in ("succeeded", "failed", "canceled")
        )
        total = summary.usage
        quantities = [
            (compact(total.model_calls) if total else "-", "model calls"),
            (compact(total.tool_calls) if total else "-", "tool calls"),
            (compact(total.input_tokens) if total else "-", "input"),
            (compact(total.cached_tokens) if total else "-", "cached"),
            (compact(total.output_tokens) if total else "-", "output"),
            (
                spend(
                    total.spend_micros / 1e6
                    if total.spend_micros is not None
                    else None,
                    total.spend_approximate,
                )
                if total
                else "-",
                "spend",
            ),
        ]
        stats = ", ".join(f"{value} {label}" for value, label in quantities)
        if unknown_inventory:
            agents += " · updating" if snapshot.inventory_updating else " · partial"
        if active_unknown or completed_unknown:
            runs += (
                " · partial"
                if any(
                    source.activity_error or source.live_stale
                    for source in snapshot.sources
                )
                else " · updating"
            )
        if total is None:
            stats += (
                " · unavailable"
                if any(source.usage_error for source in snapshot.sources)
                else " · updating"
            )
        title_width = 15
        left_width = max(1, width - title_width - 2)
        console = Console(width=left_width)
        for value, title in (
            (agents, "agents"),
            (runs, f"runs last {window(snapshot.recent)}"),
            (stats, f"stats last {window(snapshot.period)}"),
        ):
            text = Text(value, style="dim")
            text.highlight_regex(r"(?:~?\$)?\d[\d.,]*[kmhb]?|-", "bold not dim")
            wrapped = text.wrap(console, left_width)
            for index, line in enumerate(wrapped):
                line.pad_right(max(0, left_width - line.cell_len))
                line.append(
                    "  "
                    + (title.rjust(title_width) if index == 0 else " " * title_width),
                    style="dim",
                )
                lines.append(line)
        return [*lines, Text()]

    def render(
        self, snapshot: TopSnapshot, *, width: int, height: int | None = None
    ) -> Text:
        self.project(snapshot)
        lines = self.header(snapshot, width)
        agent_width = max([5, *(cell_len(row.source.label) for row in self.rows)])
        run_width = max([3, *(cell_len(row.root) for row in self.rows)])
        widths = [agent_width, run_width, 5, 5, 6, 6, 6, 7, 7]
        headings = (
            "AGENT",
            "RUN",
            "MODEL",
            "TOOL",
            "INPUT",
            "CACHED",
            "OUTPUT",
            "SPEND",
            "TIME+",
        )

        def prefix(values: list[str] | tuple[str, ...]) -> str:
            return (
                " ".join(
                    set_cell_size(value, size) if index < 2 else value.rjust(size)
                    for index, (value, size) in enumerate(zip(values, widths))
                )
                + " "
            )

        lines.append(Text(prefix(headings) + "STATUS", style="bold"))
        panel = []
        if self.help:
            panel = [
                Text(value)
                for value in (
                    "Help — F1 or Esc closes",
                    "↑/↓ select · ← fold/parent · → expand/child · PgUp/PgDn page",
                    "F3/Enter details · F5 Tree/List · F7 recent completions · F8 usage period",
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
        if height is not None:
            panel = panel[: max(1, (height - len(lines) - 2) // 2)]
            self.page_size = max(1, height - len(lines) - len(panel) - 2)
            index = next(
                (i for i, row in enumerate(self.rows) if row.key == self.selected), 0
            )
            self.offset = min(self.offset, index)
            self.offset = max(self.offset, index - self.page_size + 1)
            displayed = self.rows[self.offset : self.offset + self.page_size]
        else:
            displayed = self.rows
        for row in displayed:
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
                row.source.label,
                row.root,
                *calls,
                compact(usage.input_tokens if usage else None),
                compact(usage.cache_read_tokens if usage else None),
                compact(usage.output_tokens if usage else None),
                spend(node.cost, node.cost_approximate),
                duration(
                    node,
                    row.source.live.observed_at
                    if row.source.live_stale and row.source.live
                    else snapshot.observed_at,
                ),
            ]
            start = prefix(values)
            label = Text(
                (row.branch + row.status)
                .replace("\n", " ")
                .replace("\r", " ")
                .replace("\t", " ")
            )
            label.truncate(max(0, width - cell_len(start)), overflow="ellipsis")
            line = Text(start)
            line.append_text(label)
            if node.status in TERMINAL:
                line.stylize(
                    {"succeeded": "green", "failed": "red", "canceled": "yellow"}[
                        node.status
                    ]
                )
            if height is not None and row.key == self.selected:
                line.stylize("reverse")
            lines.append(line)
        if height is not None:
            while len(lines) < height - len(panel) - 2:
                lines.append(Text())
        lines.extend(panel)
        coverage = []
        for source in snapshot.sources:
            issues = []
            if source.live_stale:
                issues.append("live stale" if source.live else "live unavailable")
            if source.activity_error or source.usage_error:
                issues.append("source unavailable")
            if (
                source.usage is not None
                and seconds(snapshot.observed_at) - seconds(source.usage.observed_at)
                > 5
            ):
                issues.append("stats as of " + source.usage.observed_at)
            if issues:
                coverage.append(source.label + ": " + "; ".join(issues))
            if (
                source.completed is not None
                and len(source.completed.roots) < source.completed.counts.total
            ):
                coverage.append(
                    f"{source.label}: {len(source.completed.roots)}/{source.completed.counts.total} completed loaded (PgDn)"
                )
        lines.append(
            Text(" · ".join([*snapshot.inventory.diagnostics, *coverage]), style="dim")
        )
        lines.append(Text(self.footer(snapshot), style="bold"))
        return Text("\n").join(lines)
