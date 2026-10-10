"""Full-screen activity presentation; all accounting comes from observation."""

from __future__ import annotations

from datetime import datetime
import re
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.console import Console, Group
from rich.text import Text

from toolang.execution.schemas import ActivityMetrics, ActivitySnapshot
from toolang.teaming.observation import totals
from .activity_view import clean, cost, elapsed, metrics_text, tokens
from .markdown import TerminalMarkdown

if TYPE_CHECKING:
    from .activity_view import Activity, Row


def clip(text: Text, width: int, *, pad: bool = False) -> Text:
    value = text.copy()
    value.no_wrap = True
    value.overflow = "crop"
    value.truncate(width, overflow="crop", pad=pad)
    return value


def current(state: Activity, snapshot: ActivitySnapshot) -> bool:
    return (snapshot.since, snapshot.recent, snapshot.filter, snapshot.active_only) == (
        state.query.since,
        state.query.recent,
        state.query.text,
        state.query.active,
    )


def presence(state: Activity, snapshot: ActivitySnapshot) -> str:
    return "unknown" if state.reconnecting else snapshot.presence


def ends(left: Text, right: Text, width: int) -> Text:
    """Pin a suffix to the right without wrapping either half."""
    available = max(0, width - right.cell_len - 2)
    left = clip(left, available)
    return clip(
        left + Text(" " * max(0, width - left.cell_len - right.cell_len)) + right, width
    )


def header(state: Activity, width: int) -> list[Text]:
    snapshots = list(state.snapshots.values())
    updating = any(not current(state, page) for page in snapshots)
    metrics = (
        totals(snapshots)
        if not updating
        else ActivityMetrics(model=None, tool=None, cost=None, time=None)
    )
    now = datetime.now()
    if state.agent:
        snapshot = state.snapshots.get(state.agent)
        uptime = (
            now.timestamp() - snapshot.session_start
            if snapshot
            and presence(state, snapshot) == "online"
            and not snapshot.stale
            and snapshot.session_start is not None
            else None
        )
        title = Text(f"Agent uptime {elapsed(uptime)}")
    else:
        online = sum(page.presence == "online" for page in snapshots)
        unknown = state.reconnecting or any(
            page.presence == "unknown" for page in snapshots
        )
        title = Text(f"Team {'?' if unknown else online}/{len(snapshots)} online")
    lines = [ends(title, Text(now.strftime("%H:%M:%S")), width), Text("")]
    complete = (
        state.ready
        and not updating
        and not state.reconnecting
        and all(page.complete and not page.stale for page in snapshots)
    )
    groups = [
        [
            (
                "Threads",
                str(sum(p.thread_eligible for p in snapshots)) if complete else "-",
            ),
            (
                "Runs",
                str(sum(p.eligible for p in snapshots)) if complete else "-",
            ),
            ("Models", str(metrics.model) if metrics.model is not None else "-"),
            ("Tools", str(metrics.tool) if metrics.tool is not None else "-"),
        ],
        [
            ("In", tokens(metrics.input_tokens)),
            ("Cached", tokens(metrics.cached_tokens)),
            ("Out", tokens(metrics.output_tokens)),
            ("Spend", cost(metrics)),
        ],
    ]
    # Align compact columns across the two rows; never reflow on narrow screens.
    label_widths = [max(len(row[i][0]) + 1 for row in groups) for i in range(4)]
    fields = [
        [
            Text.assemble(
                (f"{label}:".ljust(label_widths[i]), "cyan"), " ", (value, "bold")
            )
            for i, (label, value) in enumerate(group)
        ]
        for group in groups
    ]
    widths = [max(row[i].cell_len for row in fields) for i in range(4)]
    for row in fields:
        line = Text("  ").join(
            clip(item, widths[i], pad=True) for i, item in enumerate(row)
        )
        lines.append(clip(line, width))
    settings = Text(
        f"Period: {state.query.since}  Recent: {state.recent_label}",
        style="dim",
    )
    if state.query.text or state.query.active:
        settings.append(
            f"  Filter: {clean(state.query.text) or '-'}  Active: {'on' if state.query.active else 'off'}"
        )
    lines += [clip(settings, width), Text("")]
    return lines


def table(
    state: Activity, rows: list[Row], width: int, once: bool
) -> tuple[Text, list[Text]]:
    numeric = {"MODEL", "TOOL", "IN", "CACHED", "OUT", "SPEND", "TIME+"}
    labels = [
        "AGENT",
        "S",
        "MODEL",
        "TOOL",
        "IN",
        "CACHED",
        "OUT",
        "SPEND",
        "TIME+",
    ]
    if state.view != "agent":
        labels += ["THREAD"]
    if state.view == "execution":
        labels += ["RUN"] + (["STEP"] if state.tree else [])
    sorted_label = {"spend": "SPEND", "time": "TIME+"}.get(state.sort, "ACTIVITY")
    headings = {
        label: label + ("↓" if label == sorted_label else "") for label in labels
    }
    data = []
    for row in rows:
        snapshot = state.snapshots[row.agent]
        stats = (
            row.stats
            if current(state, snapshot)
            else ActivityMetrics(model=None, tool=None, cost=None, time=None)
        )
        data.append(
            {
                "AGENT": row.agent.removeprefix("agent:"),
                "S": {"online": "+", "offline": "-", "unknown": "?"}[
                    presence(state, snapshot)
                ],
                "MODEL": str(stats.model) if stats.model is not None else "-",
                "TOOL": str(stats.tool) if stats.tool is not None else "-",
                "IN": tokens(stats.input_tokens),
                "CACHED": tokens(stats.cached_tokens),
                "OUT": tokens(stats.output_tokens),
                "SPEND": cost(stats),
                "TIME+": elapsed(stats.time),
                "THREAD": row.thread or "-",
                "RUN": row.root or "-",
                "STEP": row.id if row.root else "-",
            }
        )
    for label in labels:
        minimum = (
            7
            if label in {"IN", "CACHED", "OUT", "SPEND", "TIME+"}
            else len(label) + int(label in numeric)
        )
        state.column_widths[label] = max(
            state.column_widths.get(label, 0),
            minimum,
            cell_len(headings[label]),
            *(cell_len(clean(item[label])) for item in data),
        )

    def line(fields: dict[str, str], activity: str, style: str) -> Text:
        cells = []
        for label in labels:
            value = clean(fields[label])
            padding = " " * max(0, state.column_widths[label] - cell_len(value))
            cells.append(padding + value if label in numeric else value + padding)
        text = " ".join(cells) + " " + clean(activity)
        return clip(Text(text, style=style), width, pad=True)

    heading = line(
        headings,
        f"ACTIVITY({state.recent_label})" + ("↓" if sorted_label == "ACTIVITY" else ""),
        "black on green",
    )
    rendered = []
    for row, fields in zip(rows, data, strict=True):
        style = (
            "black on cyan"
            if not once and row.key == state.selected
            else "red"
            if row.node and row.node.status == "failed"
            else "dim"
            if row.placeholder or fields["S"] != "+"
            else ""
        )
        value = line(
            fields,
            row.activity if current(state, state.snapshots[row.agent]) else "-",
            style,
        )
        if state.query.text:
            value.highlight_words(
                [state.query.text], style="bold yellow", case_sensitive=False
            )
        rendered.append(value)
    return heading, rendered


def details(
    state: Activity, rows: list[Row], console: Console, width: int
) -> list[Text]:
    row = next((row for row in rows if row.key == state.selected), None)
    if state.details_selection != state.selected:
        state.details_selection = state.selected
        state.details_offset = 0
    if not state.details or row is None:
        return []
    snapshot = state.snapshots[row.agent]
    labels = [
        ("Run", row.root),
        (
            "Step",
            row.id
            if row.node and row.node.kind == "step" or row.root and row.id != row.root
            else "",
        ),
        ("Thread", row.thread),
    ]
    identity = "  ".join(f"{label}: {value}" for label, value in labels if value)
    if not identity:
        identity = f"Status: {presence(state, snapshot)}"
    elif row.node:
        identity += f"  Status: {row.node.status}"
    values = [Text(identity, style="bold")]
    if row.node and row.node.kind != "thread":
        values.append(
            Text(f"Inspect: too {row.agent.removeprefix('agent:')} inspect {row.id}")
        )
    description = row.activity
    if row.node and row.node.kind != "thread":
        description = row.node.title.removeprefix("agent::")
        if row.node.kind == "step" and row.node.summary:
            description += " · " + row.node.summary
    values.append(Text(clean(description)))
    values.append(
        Text(
            "Stats: "
            + (
                metrics_text(row.stats, exact=True)
                if current(state, snapshot)
                else "Updating"
            )
        )
    )
    values.append(
        Text(
            "Total: "
            + metrics_text(row.node.total if row.node else snapshot.total, exact=True)
        )
    )
    label, matched, eligible, loaded = (
        (
            "Threads",
            snapshot.thread_matched,
            snapshot.thread_eligible,
            len(snapshot.threads),
        )
        if state.view == "thread"
        else ("Runs", snapshot.matched, snapshot.eligible, len(snapshot.roots))
    )
    if state.query.text or state.query.active or loaded < matched:
        values.append(
            Text(
                f"{label}: {matched}/{eligible} matched  Loaded: {loaded}/{matched}",
                style="dim",
            )
        )
    if row.node and row.node.matches:
        values.append(Text("Matches: " + ", ".join(row.node.matches)))
    diagnostics = []
    if snapshot.coverage:
        diagnostics.append(snapshot.coverage)
    if snapshot.home_missing:
        diagnostics.append("Home missing")
    if snapshot.stale:
        diagnostics.append("Stale")
    if snapshot.observed is not None:
        diagnostics.append(
            "Observed: "
            + datetime.fromtimestamp(snapshot.observed)
            .astimezone()
            .isoformat(timespec="seconds")
        )
    if row.stats.estimated:
        diagnostics.append("Spend source: estimated")
    if row.stats.partial or not row.stats.complete or not row.stats.tokens_complete:
        diagnostics.append("Coverage: incomplete")
    if diagnostics:
        values.append(Text(" · ".join(diagnostics), style="dim"))
    lines = [line for value in values for line in value.wrap(console, width)]
    if state.result_key and state.result_key[:2] == row.key:
        key = (state.result_key, state.result_text, width, state.surfaces)
        if key != state.result_lines_key:
            text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", state.result_text)
            markdown = TerminalMarkdown(
                text,
                code_background=state.surfaces.code_background,
                inline_code_background=state.surfaces.inline_code_background,
                code_foreground=None,
            )
            state.result_lines = []
            for segments in console.render_lines(
                markdown, console.options.update_width(width), pad=False
            ):
                line = Text(no_wrap=True)
                for segment in segments:
                    if not segment.control:
                        line.append(segment.text, segment.style)
                state.result_lines.append(clip(line, width))
            state.result_lines_key = key
        lines += [Text("Result:", style="dim"), *state.result_lines]
    return lines


def status_bar(state: Activity, width: int) -> Text:
    if state.editor:
        prefix = clip(Text("Filter: ", style="bold"), max(0, width - 1))
        suffix = clip(
            Text(
                f"  {state.error}"
                if state.error
                else f"  Active: {state.filter_active}",
                style="red" if state.error else "bold",
            ),
            max(0, width - prefix.cell_len - 12),
        )
        value = state.buffer + "█"
        start, used = len(value), 0
        available = width - prefix.cell_len - suffix.cell_len
        for char in reversed(value):
            if used + cell_len(char) > available:
                break
            start -= 1
            used += cell_len(char)
        # A combining mark cannot retain a base character outside the viewport.
        while start < len(value) and cell_len(value[start]) == 0:
            start += 1
        return clip(
            prefix + Text(value[start:], style="bold") + suffix, width, pad=True
        )
    snapshots = list(state.snapshots.values())
    status = (
        "Connecting"
        if not state.ready
        else "Reconnecting"
        if state.reconnecting
        else "Updating"
        if any(not current(state, page) for page in snapshots)
        else ""
    )
    keys = Text(style="black on cyan")
    next_view = (
        "Threads"
        if state.view == "agent"
        else "Runs"
        if state.view == "thread"
        else "Agents"
        if state.tree
        else "Tree"
    )
    hints = [
        ("F1", "Help", state.help),
        ("F4", "Filter", False),
        ("F5", next_view.ljust(7), False),
        ("F6", "Sort", False),
        ("F7", "Recent", False),
        ("F8", "Period", False),
    ]
    for key, label, selected in hints:
        if keys.cell_len + len(key + label) + 1 + len("F10Quit") > width:
            break
        keys.append(key, style="default on default")
        keys.append(label + " ", style="bold reverse" if selected else "")
    keys.append("F10", style="default on default")
    keys.append("Quit")
    if status and keys.cell_len + len(status) + 2 <= width:
        keys.append(" " * (width - keys.cell_len - len(status)))
        keys.append(status)
    return clip(keys, width, pad=True)


def render(state: Activity, *, width: int, height: int, once: bool) -> Group:
    width, height = max(1, width), max(1, height)
    if not once and height == 1:
        return Group(status_bar(state, width))
    if not once and height == 2:
        return Group(header(state, width)[0], status_bar(state, width))
    rows = state.rows()
    state._selection(rows)
    top = header(state, width)
    console = Console(width=width)
    detail = details(state, rows, console, width)
    if state.help:
        detail = [
            Text(line)
            for line in (
                "S: + online  - offline  ? unknown. Run trees contain steps and child runs.",
                "Period controls statistics; Recent controls activity visibility. CACHED is included in IN.",
                "TIME+ accumulates execution, excluding downtime and nested durations.",
                "F7 Recent / F8 Period cycle windows. Custom: --recent 2h --since 6h or a timezone-aware timestamp.",
                "F5 names the next view: Agents / Threads / Runs / Tree; a/t/e jump to a level.",
                "Up/Down, Ctrl-P/N or mouse wheel select. Left/Right fold.",
                "Enter toggles Details; Esc closes. PgUp/PgDn page. F10, q or Ctrl-C quit.",
                "Filter: Enter applies, Esc cancels, Ctrl-A toggles Active, Ctrl-U clears.",
            )
        ]
        detail = [line for text in detail for line in text.wrap(console, width)]
    if once:
        heading, body = table(state, rows, width, once)
        return Group(
            *top, heading, *(body or [Text("No matching activity", style="dim")])
        )
    footer = status_bar(state, width)
    # Keep the identity, headings and key bar even when the terminal becomes tiny.
    top = top[: max(1, height - 3)]
    available = max(0, height - len(top) - 2)
    detail_size = min(len(detail), max(0, available // 2 - 1)) if detail else 0
    state.details_page_size = max(1, detail_size)
    state.details_offset = min(state.details_offset, max(0, len(detail) - detail_size))
    panel = []
    if detail_size:
        start = state.details_offset
        panel = [
            clip(
                Text(
                    f"{'Help' if state.help else 'Details'} {start + 1}-{start + detail_size}/{len(detail)}  PgUp/PgDn",
                    style="dim",
                ),
                width,
            ),
            *detail[start : start + detail_size],
        ]
    state.page_size = max(1, available - len(panel))
    state._selection(rows)
    # Format the viewport only; long history must not delay every key press.
    heading, body = table(
        state, rows[state.offset : state.offset + state.page_size], width, once
    )
    body = body or [Text("No matching activity", style="dim")]
    body = body[: max(0, available - len(panel))]
    blank = [Text("")] * max(0, height - len(top) - 1 - len(body) - len(panel) - 1)
    return Group(*top, heading, *body, *blank, *panel, footer)
