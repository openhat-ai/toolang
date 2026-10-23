"""Render the one-shot Hub and agent instance snapshot."""

from __future__ import annotations

import os
from dataclasses import dataclass

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from toolang.up.types import RuntimeInstance, RuntimeInstanceSnapshot


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


def _pad(line: Text, value: Text, width: int) -> None:
    line.append(value)
    padding = width - value.cell_len
    if padding > 0:
        line.append(" " * padding)


def _pad_right(line: Text, value: str, width: int, *, style: str | None = None) -> None:
    padding = width - cell_len(value)
    if padding > 0:
        line.append(" " * padding)
    line.append(value, style=style)


def _agent_label(console: Console, instance: RuntimeInstance) -> Text:
    style = {
        "resident": "bright_cyan",
        "roaming": "bright_magenta",
        "visiting": "bright_yellow",
    }[instance.placement]
    label = Text(_safe_text(console, instance.name), style=style)
    suffix = instance.label[len(instance.name) :]
    if suffix:
        label.append(_safe_text(console, suffix), style="default dim")
    return label


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
    return "not running" if instance.state == "stopped" else instance.state


def _hub_fields(snapshot: HubSnapshot) -> tuple[str, str]:
    if snapshot.state == "running":
        return "running", snapshot.version or "unknown"
    return "not running", ""


def _column_widths(
    console: Console,
    snapshot: HubSnapshot,
    *,
    width: int,
) -> tuple[int, int, int]:
    instances = snapshot.instances.instances
    hub_status, hub_version = _hub_fields(snapshot)
    tree_width = 4
    name_width = max(
        32,
        max(
            (tree_width + _agent_label(console, item).cell_len for item in instances),
            default=tree_width,
        ),
    )
    status_width = max(
        11,
        cell_len(hub_status),
        max(
            (cell_len(_safe_text(console, _status_label(item))) for item in instances),
            default=11,
        ),
    )
    talk_width = max(
        8,
        cell_len(_safe_text(console, hub_version)),
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


def _row_lines(
    console: Console,
    name: Text,
    status: str,
    detail: str,
    *,
    name_width: int,
    status_width: int,
    talk_width: int,
    prefix: str = "",
    continuation: str = "",
) -> list[Text]:
    label_width = max(1, name_width - cell_len(prefix))
    name_chunks = name.wrap(console, label_width, overflow="fold")
    status = _safe_text(console, status)
    detail = _safe_text(console, detail)
    status_chunks = _wrap(console, status, status_width)
    talk_chunks = _wrap(console, detail, talk_width)
    row_count = max(len(name_chunks), len(status_chunks), len(talk_chunks))
    status_style = "dim" if status in {"not running", "-"} else ""
    talk_style = "dim" if detail in {"disabled", "-", "unknown"} else None
    lines: list[Text] = []

    for row_index in range(row_count):
        line = Text(no_wrap=True)
        line.append(prefix if row_index == 0 else continuation)

        name_chunk = name_chunks[row_index] if row_index < len(name_chunks) else Text()
        _pad(line, name_chunk, label_width)
        line.append("  ")

        status_chunk = (
            status_chunks[row_index] if row_index < len(status_chunks) else ""
        )
        _pad(line, Text(status_chunk, style=status_style), status_width)
        line.append("  ")

        talk_chunk = talk_chunks[row_index] if row_index < len(talk_chunks) else ""
        _pad_right(line, talk_chunk, talk_width, style=talk_style)
        lines.append(Text(line.plain, no_wrap=True) if console.no_color else line)

    return lines


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
    is_last = index == count - 1
    branch = (
        ("\\--" if is_last else "+--") if ascii_tree else ("└──" if is_last else "├──")
    )
    spine = "|   " if ascii_tree else "│   "
    return _row_lines(
        console,
        _agent_label(console, instance),
        _status_label(instance),
        _talk_label(instance),
        name_width=name_width,
        status_width=status_width,
        talk_width=talk_width,
        prefix=f"{branch} ",
        continuation="    " if is_last else spine,
    )


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
    name_width, status_width, talk_width = _column_widths(
        console, snapshot, width=max(20, console.width)
    )
    hub_status, hub_version = _hub_fields(snapshot)
    for line in _row_lines(
        console,
        Text("hub", style="bright_cyan"),
        hub_status,
        hub_version,
        name_width=name_width,
        status_width=status_width,
        talk_width=talk_width,
    ):
        console.print(line)

    if instances:
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
        branch = "\\--" if ascii_tree else "└──"
        style = "" if console.no_color else "default dim"
        console.print(Text(f"{branch} no agents", style=style, no_wrap=True))

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
