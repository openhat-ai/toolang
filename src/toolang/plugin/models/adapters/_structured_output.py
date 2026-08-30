"""Shared structured-output request helpers for built-in model adapters."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from typing import cast


def append_structured_output_directive(
    instructions: str,
    schema: Mapping[str, object],
) -> str:
    """Add one deterministic schema directive to provider-wire instructions."""

    schema_text = json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    directive = (
        "<structured-output>\n"
        "Return exactly one unwrapped JSON value matching this JSON Schema. "
        "Do not include a preface, explanation, or Markdown fence.\n"
        f"{schema_text}\n"
        "</structured-output>"
    )
    return (
        f"{instructions.rstrip()}\n\n{directive}" if instructions.strip() else directive
    )


def openai_strict_object_schema(
    schema: Mapping[str, object],
) -> dict[str, object] | None:
    """Return an OpenAI strict-compatible root object schema when possible."""

    copied = deepcopy(dict(schema))
    raw_definitions = copied.get("$defs", {})
    if not isinstance(raw_definitions, Mapping):
        return None
    definitions = {str(name): value for name, value in raw_definitions.items()}
    for definition in definitions.values():
        if not isinstance(definition, Mapping) or not _is_strict_schema_node(
            cast(Mapping[str, object], definition),
            definitions=definitions,
        ):
            return None

    root: dict[str, object]
    if "$ref" in copied:
        if set(copied) != {"$defs", "$ref"}:
            return None
        name = _local_definition_name(copied["$ref"])
        definition = definitions.get(name) if name is not None else None
        if not isinstance(definition, Mapping):
            return None
        root = {
            "$defs": definitions,
            **dict(cast(Mapping[str, object], definition)),
        }
    else:
        root = copied

    if root.get("type") != "object":
        return None
    if not _is_strict_schema_node(root, definitions=definitions):
        return None
    return root


def _is_strict_schema_node(
    schema: Mapping[str, object],
    *,
    definitions: Mapping[str, object],
) -> bool:
    if not schema:
        return False
    if "$ref" in schema:
        name = _local_definition_name(schema.get("$ref"))
        return set(schema) == {"$ref"} and name in definitions

    schema_type = schema.get("type")
    if schema_type == "object":
        if not set(schema) <= {
            "$defs",
            "additionalProperties",
            "properties",
            "required",
            "type",
        }:
            return False
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            return False
        if schema.get("additionalProperties") is not False:
            return False
        if any(not isinstance(name, str) for name in required):
            return False
        if set(cast(list[str], required)) != {str(name) for name in properties}:
            return False
        return all(
            isinstance(value, Mapping)
            and _is_strict_schema_node(
                cast(Mapping[str, object], value),
                definitions=definitions,
            )
            for value in properties.values()
        )
    if schema_type == "array":
        if set(schema) != {"items", "type"}:
            return False
        items = schema.get("items")
        return isinstance(items, Mapping) and _is_strict_schema_node(
            cast(Mapping[str, object], items),
            definitions=definitions,
        )
    return schema_type in {"boolean", "number", "string"} and set(schema) == {"type"}


def _local_definition_name(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("#/$defs/"):
        return None
    name = value.removeprefix("#/$defs/")
    return name or None
