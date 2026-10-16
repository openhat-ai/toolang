"""Render the one-shot Hub and agent instance snapshot."""

from __future__ import annotations

import os
from dataclasses import dataclass

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from toolang.up.observation import RuntimeInstance, RuntimeInstanceSnapshot


@dataclass(frozen=True, slots=True)
class HubSnapshot:
    """Hub state combined with the local runtime roster."""

    state: str
    version: str | None
    instances: RuntimeInstanceSnapshot
    diagnostic: str | None = None

    @property
    def partial(self) -> bool:
        return self.diagnostic is not None or self.instances.partial


def _safe_text(console: Console, value: str) -> str:
    encoding = getattr(console.file, "encoding", None)
    if not encoding:
        return value
    try:
        value.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return value.encode("ascii", "backslashreplace").decode("ascii")
    return value


def _wrap(console: Console, value: str, width: int) -> list[str]:
    value = _safe_text(console, value)
    if width <= 0:
        return [value]
    return [line.plain for line in Text(value).wrap(console, width, overflow="fold")]


def _pad(line: Text, value: str, width: int, *, style: str | None = None) -> None:
    line.append(value, style=style)
    padding = width - cell_len(value)
    if padding > 0:
        line.append(" " * padding)


def _ascii_tree(console: Console) -> bool:
    if os.environ.get("TERM", "") == "dumb":
        return True
    encoding = getattr(console.file, "encoding", None)
    if not encoding:
        return False
    try:
        "│├╌└".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return True
    return False


def _columns(width: int, tree_width: int) -> tuple[int, int, int, int]:
    hub_width = min(26, max(6, width // 3))
    right_width = max(5, width - hub_width - tree_width)
    usable = max(3, right_width - 2)
    if usable >= 21:
        status_width = max(7, round(usable * 0.28))
        talk_width = max(7, round(usable * 0.16))
        agent_width = usable - status_width - talk_width
    else:
        agent_width = max(1, round(usable * 0.56))
        status_width = max(1, round(usable * 0.27))
        talk_width = max(1, usable - agent_width - status_width)
    while agent_width + status_width + talk_width > usable:
        if agent_width > 1:
            agent_width -= 1
        elif status_width > 1:
            status_width -= 1
        else:
            talk_width -= 1
    return hub_width, agent_width, status_width, talk_width


def _append_header(
    console: Console,
    *,
    hub_width: int,
    tree_width: int,
    agent_width: int,
    status_width: int,
    talk_width: int,
) -> list[Text]:
    fields = (("AGENT", agent_width), ("STATUS", status_width), ("TALK", talk_width))
    chunks = [(_wrap(console, name, width), width) for name, width in fields]
    count = max(len(lines) for lines, _ in chunks)
    output: list[Text] = []
    for index in range(count):
        line = Text(no_wrap=True)
        if index == 0:
            _pad(line, "HUB", hub_width, style="dim")
        else:
            line.append(" " * hub_width)
        line.append(" " * tree_width)
        for field_index, (lines, width) in enumerate(chunks):
            value = lines[index] if index < len(lines) else ""
            if field_index == len(chunks) - 1:
                line.append(value, style="dim")
            else:
                _pad(line, value, width, style="dim")
            if field_index < len(chunks) - 1:
                line.append(" ")
        output.append(line)
    return output


def _agent_row_lines(
    console: Console,
    instance: RuntimeInstance,
    *,
    is_last: bool,
    agent_width: int,
    status_width: int,
    talk_width: int,
    ascii_tree: bool,
) -> list[Text]:
    state = instance.state
    talk = (
        "enabled"
        if state == "running"
        and instance.drivers is not None
        and "talk" in instance.drivers
        else "disabled"
        if state == "running" and instance.drivers is not None
        else "-"
    )
    visible_state = "-" if state == "unknown" else state
    fields = (
        _wrap(console, instance.label, agent_width),
        _wrap(console, visible_state, status_width),
        _wrap(console, talk, talk_width),
    )
    line_count = max(len(lines) for lines in fields)
    if ascii_tree:
        solid_branch = "\\-" if is_last else "+-"
        dashed_branch = "\\." if is_last else "+."
        spine = "| "
    else:
        solid_branch = "└─" if is_last else "├─"
        dashed_branch = "└╌" if is_last else "├╌"
        spine = "│ "
    branch = solid_branch if talk == "enabled" else dashed_branch
    row_style = None if state == "running" else "dim"
    output: list[Text] = []
    for index in range(line_count):
        line = Text(no_wrap=True)
        line_style = row_style if index == 0 else "dim"
        if index == 0:
            line.append(branch, style=line_style)
        else:
            line.append(("  " if is_last else spine), style=line_style)
        for field_index, (values, width) in enumerate(
            zip(fields, (agent_width, status_width, talk_width), strict=True)
        ):
            value = values[index] if index < len(values) else ""
            if field_index == 2:
                line.append(value, style=line_style)
            else:
                _pad(line, value, width, style=line_style)
            if field_index < 2:
                line.append(" ")
        output.append(line)
    return output


def _small_table_lines(
    console: Console, snapshot: HubSnapshot, *, width: int, ascii_tree: bool
) -> list[Text]:
    """Stack state fields when a 20-column terminal cannot fit the full table."""
    hub_width = min(6, max(4, width // 3))
    tree_width = 2
    value_width = max(1, width - hub_width - tree_width)
    if ascii_tree:
        spine = "| "
    else:
        spine = "│ "
    headers = [Text(no_wrap=True), Text(no_wrap=True)]
    _pad(headers[0], "HUB", hub_width, style="dim")
    headers[0].append(" " * tree_width)
    _pad(headers[0], "AGENT", value_width, style="dim")
    headers[1].append(" " * (hub_width + tree_width))
    _pad(headers[1], "STATUS/TALK", value_width, style="dim")

    state = snapshot.state
    if snapshot.version:
        state_separator = " | " if ascii_tree else " · "
        state = f"{state}{state_separator}{snapshot.version}"
    state = _safe_text(console, state)
    left_block = ["Hub", *_wrap(console, state, hub_width)]
    right_block: list[Text] = []
    instances = snapshot.instances.instances
    if not instances:
        right_block.append(Text("  No agents", style="dim", no_wrap=True))
        right_block.append(Text("", no_wrap=True))
    else:
        for index, item in enumerate(instances):
            last = index == len(instances) - 1
            status = "-" if item.state == "unknown" else item.state
            talk = (
                "enabled"
                if item.state == "running"
                and item.drivers is not None
                and "talk" in item.drivers
                else "disabled"
                if item.state == "running" and item.drivers is not None
                else "-"
            )
            if ascii_tree:
                branch = (
                    ("\\-" if last else "+-")
                    if talk == "enabled"
                    else ("\\." if last else "+.")
                )
            else:
                branch = (
                    ("└─" if last else "├─")
                    if talk == "enabled"
                    else ("└╌" if last else "├╌")
                )
            style = None if item.state == "running" else "dim"
            label_chunks = _wrap(console, item.label, value_width)
            for chunk_index, chunk in enumerate(label_chunks):
                line = Text(no_wrap=True)
                chunk_style = style if chunk_index == 0 else "dim"
                line.append(
                    branch if chunk_index == 0 else ("  " if last else spine),
                    style=chunk_style,
                )
                line.append(chunk, style=chunk_style)
                right_block.append(line)
            state_chunks = _wrap(console, f"{status} / {talk}", value_width)
            for chunk in state_chunks:
                line = Text(no_wrap=True)
                line.append("  " if last else spine, style="dim")
                line.append(chunk, style="dim")
                right_block.append(line)

    lines = list(headers)
    count = max(len(left_block), len(right_block))
    for index in range(count):
        line = Text(no_wrap=True)
        left = left_block[index] if index < len(left_block) else ""
        right = right_block[index] if index < len(right_block) else Text("")
        has_right = bool(right.plain)
        if isinstance(left, Text):
            line.append(left)
            left_width = cell_len(left.plain)
            if has_right and left_width < hub_width:
                line.append(" " * (hub_width - left_width))
        elif has_right:
            _pad(line, left, hub_width, style="dim" if index else None)
        else:
            line.append(left, style="dim" if index else None)
        if has_right:
            line.append(right)
        lines.append(line)
    return lines


def _table_lines(
    console: Console, snapshot: HubSnapshot, *, width: int, ascii_tree: bool
) -> list[Text]:
    if width < 32:
        return _small_table_lines(console, snapshot, width=width, ascii_tree=ascii_tree)
    tree_width = 2
    hub_width, agent_width, status_width, talk_width = _columns(width, tree_width)
    header = _append_header(
        console,
        hub_width=hub_width,
        tree_width=tree_width,
        agent_width=agent_width,
        status_width=status_width,
        talk_width=talk_width,
    )
    state = snapshot.state
    if snapshot.version:
        state_separator = " | " if ascii_tree else " · "
        state = f"{state}{state_separator}{snapshot.version}"
    state = _safe_text(console, state)
    left_block = ["Hub", *_wrap(console, state, hub_width)]

    if snapshot.instances.instances:
        right_block: list[Text] = []
        for index, instance in enumerate(snapshot.instances.instances):
            right_block.extend(
                _agent_row_lines(
                    console,
                    instance,
                    is_last=index == len(snapshot.instances.instances) - 1,
                    agent_width=agent_width,
                    status_width=status_width,
                    talk_width=talk_width,
                    ascii_tree=ascii_tree,
                )
            )
    else:
        right_block = [
            Text("  No agents", style="dim", no_wrap=True),
            Text("", no_wrap=True),
        ]

    count = max(len(left_block), len(right_block))
    lines = list(header)
    for index in range(count):
        line = Text(no_wrap=True)
        left = left_block[index] if index < len(left_block) else ""
        right = right_block[index] if index < len(right_block) else Text("")
        has_right_content = bool(right.plain)
        if isinstance(left, Text):
            line.append(left)
            left_width = cell_len(left.plain)
            if has_right_content and left_width < hub_width:
                line.append(" " * (hub_width - left_width))
        elif has_right_content:
            _pad(line, left, hub_width, style="dim" if index else None)
        else:
            line.append(left, style="dim" if index else None)
        if has_right_content:
            line.append(right)
        lines.append(line)
    return lines


def render_hub_snapshot(snapshot: HubSnapshot, console: Console | None = None) -> None:
    """Print the status summary, Hub/agent tree, and concise partial diagnostics."""
    console = console or Console()
    instances = snapshot.instances.instances
    running = sum(item.state == "running" for item in instances)
    talk_enabled = sum(
        item.state == "running" and item.drivers is not None and "talk" in item.drivers
        for item in instances
    )
    ascii_tree = _ascii_tree(console)
    separator = " | " if ascii_tree else " · "
    summary = (
        f"hub {snapshot.state}{separator}{len(instances)} agents{separator}{running} running"
        f"{separator}{talk_enabled} talk enabled"
    )
    summary = _safe_text(console, summary)
    if snapshot.partial:
        summary += f"{separator}partial"
    console.print(Text(summary, no_wrap=True), soft_wrap=True)
    console.print()

    width = max(20, console.width)
    table = _table_lines(console, snapshot, width=width, ascii_tree=ascii_tree)
    if (
        console.is_terminal
        and len(instances) <= 10
        and console.height >= len(table) + 2
    ):
        padding = max(0, (console.height - len(table) - 2) // 2)
        for _ in range(padding):
            console.print()
    for line in table:
        console.print(line)

    diagnostics = [
        f"{item.label}: {item.observed_state}: {item.diagnostic}"
        if item.observed_state
        else f"{item.label}: {item.diagnostic}"
        for item in instances
        if item.diagnostic is not None
    ]
    diagnostics.extend(snapshot.instances.diagnostics)
    if snapshot.diagnostic is not None:
        diagnostics.append(f"Hub: {snapshot.diagnostic}")
    for diagnostic in diagnostics:
        for line in _wrap(console, f"partial: {diagnostic}", width):
            console.print(Text(line, style="dim", no_wrap=True))
