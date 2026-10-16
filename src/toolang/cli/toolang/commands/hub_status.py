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


def _pad_right(line: Text, value: str, width: int, *, style: str | None = None) -> None:
    padding = width - cell_len(value)
    if padding > 0:
        line.append(" " * padding)
    line.append(value, style=style)


def _ascii_tree(console: Console) -> bool:
    if os.environ.get("TERM", "") == "dumb":
        return True
    encoding = getattr(console.file, "encoding", None)
    if not encoding:
        return False
    try:
        "│├└".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return True
    return False


def _talk_label(instance: RuntimeInstance) -> str:
    if instance.state == "running" and instance.drivers is not None:
        return "enabled" if "talk" in instance.drivers else "disabled"
    return "-"


def _status_label(instance: RuntimeInstance) -> str:
    return "-" if instance.state == "unknown" else instance.state


def _column_widths(
    console: Console,
    instances: tuple[RuntimeInstance, ...],
    *,
    width: int,
) -> tuple[int, int, int]:
    tree_width = 4
    name_width = max(
        24,
        max(
            (
                tree_width + cell_len(_safe_text(console, item.label))
                for item in instances
            ),
            default=tree_width,
        ),
    )
    status_width = max(
        10,
        max(
            (cell_len(_safe_text(console, _status_label(item))) for item in instances),
            default=10,
        ),
    )
    talk_width = max(
        8,
        max(
            (cell_len(_safe_text(console, _talk_label(item))) for item in instances),
            default=8,
        ),
    )
    if name_width + status_width + talk_width + 4 <= width:
        return name_width, status_width, talk_width

    available = max(3, width - 4)
    talk_width = min(talk_width, max(1, available // 5))
    status_width = min(status_width, max(1, (available - talk_width) // 3))
    name_width = max(1, available - status_width - talk_width)
    return name_width, status_width, talk_width


def _agent_lines(
    console: Console,
    instance: RuntimeInstance,
    *,
    index: int,
    count: int,
    name_width: int,
    status_width: int,
    talk_width: int,
    ascii_tree: bool,
) -> list[Text]:
    tree_width = 4
    is_last = index == count - 1
    branch = (
        ("\\--" if is_last else "+--") if ascii_tree else ("└──" if is_last else "├──")
    )
    spine = "|   " if ascii_tree else "│   "
    name_chunks = _wrap(console, instance.label, max(1, name_width - tree_width))
    status = _safe_text(console, _status_label(instance))
    talk = _safe_text(console, _talk_label(instance))
    status_chunks = _wrap(console, status, status_width)
    talk_chunks = _wrap(console, talk, talk_width)
    row_count = max(len(name_chunks), len(status_chunks), len(talk_chunks))
    status_style = "dim" if status in {"not running", "-"} else None
    talk_style = "dim" if talk in {"disabled", "-"} else None
    lines: list[Text] = []

    for row_index in range(row_count):
        line = Text(no_wrap=True)
        if row_index == 0:
            line.append(f"{branch} ")
        else:
            line.append("    " if is_last else spine)

        name = name_chunks[row_index] if row_index < len(name_chunks) else ""
        _pad(line, name, max(1, name_width - tree_width))
        line.append("  ")

        status_chunk = (
            status_chunks[row_index] if row_index < len(status_chunks) else ""
        )
        _pad(line, status_chunk, status_width, style=status_style)
        line.append("  ")

        talk_chunk = talk_chunks[row_index] if row_index < len(talk_chunks) else ""
        _pad_right(line, talk_chunk, talk_width, style=talk_style)
        lines.append(line)

    return lines


def _hub_line(snapshot: HubSnapshot, console: Console) -> Text:
    line = Text(no_wrap=True)
    line.append("hub ")
    if snapshot.state == "running":
        line.append(_safe_text(console, snapshot.version or "running"))
    else:
        line.append("not running")
    return line


def render_hub_snapshot(snapshot: HubSnapshot, console: Console | None = None) -> None:
    """Print Hub availability, a three-column agent tree, and summary counts."""
    console = console or Console()
    instances = snapshot.instances.instances
    running = sum(item.state == "running" for item in instances)
    talk_enabled = sum(
        item.state == "running" and item.drivers is not None and "talk" in item.drivers
        for item in instances
    )
    ascii_tree = _ascii_tree(console)
    console.print(_hub_line(snapshot, console))

    if instances:
        width = max(20, console.width)
        name_width, status_width, talk_width = _column_widths(
            console, instances, width=width
        )
        for index, instance in enumerate(instances):
            for line in _agent_lines(
                console,
                instance,
                index=index,
                count=len(instances),
                name_width=name_width,
                status_width=status_width,
                talk_width=talk_width,
                ascii_tree=ascii_tree,
            ):
                console.print(line)
    else:
        console.print(Text("no agents", no_wrap=True))

    if instances:
        console.print()
        summary = (
            f"{len(instances)} agents, {running} running, {talk_enabled} talk enabled"
        )
        if snapshot.partial:
            summary += ", partial"
        console.print(Text(_safe_text(console, summary), no_wrap=True), soft_wrap=True)

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
        for line in _wrap(console, f"partial: {diagnostic}", max(20, console.width)):
            console.print(Text(line, no_wrap=True))
