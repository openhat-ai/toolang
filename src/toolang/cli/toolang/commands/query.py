"""Data-independent collection-query help."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from typer._click.exceptions import UsageError

from toolang.cli.common.parameters import TextType

from toolang.common.query import CollectionSchema
from toolang.plugin.models.collections import MODEL_SCHEMA
from toolang.plugin.toolsets.collections import TOOL_SCHEMA
from toolang.state.collections import cap_kind_definition
from .metadata import QUERY_HELP

COLLECTIONS = ("models", "tools", "psyches", "skills", "services", "prompts")


def query_command(
    collection: Annotated[
        str | None,
        typer.Argument(
            metavar="COLLECTION",
            click_type=TextType(),
            help="Base collection whose query fields to show",
        ),
    ] = None,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write the query schema as JSON"),
    ] = False,
) -> None:
    """Show generic or collection-specific query help."""

    if collection is None:
        if json_:
            raise UsageError("--json requires COLLECTION")
        typer.echo(QUERY_HELP.strip())
        return
    schemas = _schemas()
    schema = schemas.get(collection)
    if schema is None:
        supported = ", ".join(COLLECTIONS)
        raise typer.BadParameter(
            f"unknown query collection {collection!r}; supported: {supported}",
            param_hint="COLLECTION",
        )
    if collection == "models":
        typer.echo(_model_query_json(schema) if json_ else _model_query_help(schema))
    else:
        typer.echo(schema.to_json() if json_ else schema.help_text())


def _model_query_help(schema: CollectionSchema[Any]) -> str:
    """Show model fields with tq-json syntax, not collection-index operators."""

    fields = "\n".join(
        f"  {name}: {field.type_name}" for name, field in schema.fields.items()
    )
    return (
        "Collection: models (tq-json)\n"
        "Identity: bare glob matches model id; provider/model matches a full ref\n"
        "Query: comma-separated OR branches; predicates in [...] are AND-ed\n"
        "Predicates: =, !=, <, <=, >, >=, has, has no, has any/all/none; see "
        "https://pypi.org/project/tq-json/\n"
        "Sequence = is membership; != requires a nonempty sequence without the value\n"
        f"Fields:\n{fields}"
    )


def _model_query_json(schema: CollectionSchema[Any]) -> str:
    """Return model field metadata with the query engine made explicit."""

    data = json.loads(schema.to_json())
    data["query_language"] = "tq-json"
    data["identity"] = {
        "key": "ref",
        "bare_glob": "model id across providers",
        "full_ref": "provider/model id",
    }
    for field in data["fields"]:
        field.pop("operators", None)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _schemas() -> dict[str, CollectionSchema[Any]]:
    return {
        "models": MODEL_SCHEMA,
        "tools": TOOL_SCHEMA,
        "psyches": cap_kind_definition("psyche").schema,
        "skills": cap_kind_definition("skill").schema,
        "services": cap_kind_definition("service").schema,
        "prompts": cap_kind_definition("prompt").schema,
    }


__all__ = ["COLLECTIONS", "QUERY_HELP", "query_command"]
