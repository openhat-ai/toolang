"""Durable execution record schema discovery."""

from __future__ import annotations

from fnmatch import fnmatchcase
import json
from typing import Annotated, Any, cast

import typer

from toolang.cli.common.output import echo_table
from toolang.common.selectors import split_selector_list
from toolang.execution.schemas import (
    record_kinds,
    record_schema,
    record_variants,
    records_schema,
)


_RECORD_NAMES = {
    "thread": "ThreadRecord",
    "control": "ControlRecord",
    "run": "RunRecord",
    "step": "StepRecord",
}
_RECORD_REFS = {
    "thread": "THREAD_ID",
    "control": "(THREAD_ID|RUN_ID)^INDEX",
    "run": "RUN_ID",
    "step": "RUN_ID.INDEX...",
}


def records_command(
    filter_: Annotated[
        list[str] | None,
        typer.Option(
            "--filter",
            "-f",
            help="Filter record kinds. Pass CSV or repeat.",
        ),
    ] = None,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write canonical record JSON Schema."),
    ] = False,
) -> None:
    """List canonical durable record schemas."""

    selected = _selected_kinds(filter_)
    if not selected:
        typer.echo("No matched records.")
        return
    if json_:
        schema = (
            record_schema(selected[0])
            if len(selected) == 1
            else records_schema(selected)
        )
        typer.echo(
            json.dumps(
                schema,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return
    rows: list[tuple[str, str, str, str, str]] = []
    for kind in selected:
        schema = record_schema(kind)
        properties = cast(dict[str, dict[str, Any]], schema.get("properties", {}))
        for field, field_schema in properties.items():
            rows.append(
                (
                    _RECORD_NAMES[kind],
                    _RECORD_REFS[kind],
                    f"/{_escape_token(field)}",
                    _field_type(kind, field, field_schema),
                    "yes" if _nullable(field_schema) else "no",
                )
            )
        if len(selected) == 1:
            rows.extend(
                (
                    f"  {schema_name}",
                    "",
                    f"/{field} ({variant})",
                    schema_name,
                    "",
                )
                for field, variant, schema_name in record_variants(kind)
            )
    echo_table(
        ("RECORD", "RECORD REF", "FIELD REF", "TYPE", "NULLABLE"),
        rows,
    )


def _selected_kinds(filters: list[str] | None) -> tuple[str, ...]:
    patterns = split_selector_list(filters or ()) or ("*",)
    return tuple(
        kind
        for kind in record_kinds()
        if any(fnmatchcase(kind, pattern) for pattern in patterns)
    )


def _schema_type(schema: dict[str, Any]) -> str:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        return reference.rsplit("/", 1)[-1]
    variants = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(variants, list):
        names = [
            name
            for variant in variants
            if isinstance(variant, dict)
            and (name := _schema_type(cast(dict[str, Any], variant))) != "null"
        ]
        return " | ".join(dict.fromkeys(names)) or "null"
    type_name = schema.get("type")
    if type_name == "array":
        items = schema.get("items")
        return (
            f"{_schema_type(cast(dict[str, Any], items))}[]"
            if isinstance(items, dict)
            else "array"
        )
    return str(type_name or schema.get("title") or "value")


def _field_type(kind: str, field: str, schema: dict[str, Any]) -> str:
    owned_union = {
        ("control", "payload"): "ControlPayload",
        ("step", "given"): "StoredStepGiven",
    }.get((kind, field))
    return owned_union or _schema_type(schema)


def _nullable(schema: dict[str, Any]) -> bool:
    variants = schema.get("anyOf") or schema.get("oneOf")
    return isinstance(variants, list) and any(
        isinstance(variant, dict) and variant.get("type") == "null"
        for variant in variants
    )


def _escape_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")
