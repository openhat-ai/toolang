"""JSON records and tables sharing the same public field paths."""

import json
from os.path import relpath
from pathlib import Path
from shutil import get_terminal_size
from collections.abc import Mapping, Sequence
from typing import cast

from rich.cells import cell_len, chop_cells
from rich.console import Console
from rich.text import Text
from wcwidth import iter_graphemes_reverse, wcswidth

import typer
from typer._click.exceptions import UsageError

from .output import MIN_TABLE_WIDTH, _table_cell_text, echo_table
from .terminal_title import clip_title, safe_text


def check_output_options(*, human: bool, json_: bool) -> None:
    if human and json_:
        raise UsageError("--human and --json are mutually exclusive")


def echo_compact_records(
    records: Sequence[Mapping[str, object]],
    columns: Sequence[str],
    *,
    heading: str,
    json_: bool,
    root: Path | None = None,
) -> None:
    """Fit descriptive records into single rows using the current terminal width."""
    if json_:
        echo_records(records, columns, json_=True)
        return
    if not records:
        return
    width = max(MIN_TABLE_WIDTH, get_terminal_size(fallback=(120, 24)).columns)
    console = Console(width=width, highlight=False)
    headers = [
        heading,
        *("STATUS" if column == "tags" else column.upper() for column in columns[1:]),
    ]
    rows = [
        tuple(
            _compact_cell(record.get(column), column=column, root=root)
            for column in columns
        )
        for record in records
    ]
    gap = 2
    minimums = [len(header) for header in headers]
    widths = [
        max(len(header), *(wcswidth(row[index]) for row in rows))
        for index, header in enumerate(headers)
    ]
    excess = max(0, sum(widths) + gap * (len(columns) - 1) - width)
    flexible = [
        index
        for index, column in enumerate(columns)
        if column in {"description", "title", "location"}
    ]
    capacity = sum(widths[index] - minimums[index] for index in flexible)
    reduction = min(excess, capacity)
    remaining = reduction
    for index in flexible:
        share = (
            reduction * (widths[index] - minimums[index]) // capacity if capacity else 0
        )
        widths[index] -= share
        remaining -= share
    for index in flexible:
        if remaining and widths[index] > minimums[index]:
            widths[index] -= 1
            remaining -= 1

    def print_row(values: Sequence[str], *, header: bool = False) -> None:
        line = Text()
        for index, (value, column_width) in enumerate(zip(values, widths)):
            cell = (
                _clip_location(value, column_width)
                if not header and columns[index] == "location"
                else clip_title(value, width=column_width)
            )
            line.append(Text(cell) if header else _table_cell_text(cell))
            if index < len(columns) - 1:
                line.append(" " * (column_width - wcswidth(cell) + gap))
        console.print(line, soft_wrap=True)

    print_row(headers, header=True)
    console.print(
        Text("─" * (sum(widths) + gap * (len(columns) - 1)), style="dim"),
        soft_wrap=True,
    )
    for row in rows:
        print_row(row)


def _compact_cell(value: object, *, column: str, root: Path | None) -> str:
    if column == "tags":
        value = (
            [tag for tag in value if tag in {"ready", "not_allowed"}]
            if isinstance(value, (list, tuple))
            else []
        )
    elif (
        column == "location"
        and root is not None
        and isinstance(value, str)
        and Path(value).is_absolute()
    ):
        value = relpath(value, root)
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        value = ",".join(str(item) for item in value)
    text = safe_text(
        str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    )
    return (
        " ".join(text.split()) if column in {"description", "title"} else text
    ) or "-"


def _clip_location(value: str, width: int) -> str:
    if wcswidth(value) <= width:
        return value
    prefix = clip_title(value, width=(width - 1) // 2 + 1).removesuffix("…")
    budget = width - wcswidth(prefix) - 1
    suffix = []
    for cluster in iter_graphemes_reverse(value):
        size = max(0, wcswidth(cluster))
        if size > budget:
            break
        suffix.append(cluster)
        budget -= size
    return prefix + "…" + "".join(reversed(suffix))


def echo_records(
    records: Sequence[Mapping[str, object]],
    columns: Sequence[str],
    *,
    json_: bool,
    align_ref_continuations: bool = False,
    right_align: Sequence[str] = (),
) -> None:
    if json_:
        typer.echo(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
        return

    def cell(record: Mapping[str, object], path: str) -> str:
        value: object = record
        for part in path.split("."):
            value = (
                cast(Mapping[str, object], value).get(part)
                if isinstance(value, Mapping)
                else None
            )
        if value is None:
            return "-"
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, (list, tuple)):
            return ",".join(str(item) for item in value) or "-"
        if path in {"context", "max_output"} and isinstance(value, int):
            return f"{value:,}"
        text = str(value)
        if path in {"ref", "cap"}:
            return _wrap_ref(text)
        if path in {"location", "price", "models"}:
            return text
        return text[:117] + "..." if len(text) > 120 else text

    if records:
        rows = [tuple(cell(record, path) for path in columns) for record in records]
        if align_ref_continuations and "ref" in columns:
            ref_index = columns.index("ref")
            width = max(
                len("REF"),
                *(
                    cell_len(line)
                    for row in rows
                    for line in row[ref_index].split("\n")
                ),
            )
            for index, row in enumerate(rows):
                lines = row[ref_index].split("\n")
                if len(lines) == 1:
                    continue
                ref = "\n".join(
                    [
                        lines[0],
                        *(" " * (width - cell_len(line)) + line for line in lines[1:]),
                    ]
                )
                rows[index] = tuple(
                    ref if column == ref_index else "\n" + value
                    for column, value in enumerate(row)
                )
        echo_table(
            tuple(column.upper() for column in columns),
            rows,
            justify=tuple(
                "right" if column in right_align else "left" for column in columns
            ),
            max_widths=tuple(
                40 if column in {"ref", "cap"} else None for column in columns
            ),
        )


def _wrap_ref(value: str) -> str:
    """Wrap without losing characters, preferring slash boundaries within 40 cells."""

    lines = []
    while value:
        chunk = chop_cells(value, 40)[0]
        if len(chunk) < len(value):
            boundary = chunk.rfind("/")
            if boundary >= 0:
                chunk = chunk[: boundary + 1]
        lines.append(chunk)
        value = value[len(chunk) :]
    return "\n".join(lines)
