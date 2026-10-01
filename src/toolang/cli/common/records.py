"""JSON records and tables sharing the same public field paths."""

import json
from collections.abc import Mapping, Sequence
from typing import cast

from rich.cells import chop_cells

import typer
from typer._click.exceptions import UsageError

from .output import echo_table


def check_output_options(*, human: bool, json_: bool) -> None:
    if human and json_:
        raise UsageError("--human and --json are mutually exclusive")


def echo_records(
    records: Sequence[Mapping[str, object]], columns: Sequence[str], *, json_: bool
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
        text = str(value)
        if path == "ref":
            return _wrap_ref(text)
        if path in {"location", "price", "models"}:
            return text
        return text[:117] + "..." if len(text) > 120 else text

    if records:
        echo_table(
            tuple(column.upper() for column in columns),
            [tuple(cell(record, path) for path in columns) for record in records],
            max_widths=tuple(40 if column == "ref" else None for column in columns),
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
