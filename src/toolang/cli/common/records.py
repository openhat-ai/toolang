"""JSON records and tables sharing the same public field paths."""

import json
from collections.abc import Mapping, Sequence
from typing import cast

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
        return text[:117] + "..." if len(text) > 120 else text

    if records:
        echo_table(
            tuple(columns),
            [tuple(cell(record, path) for path in columns) for record in records],
        )
