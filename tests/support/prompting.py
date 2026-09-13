"""Focused resident-instruction inputs without filesystem or provider setup."""

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast

from toolang.execution.assembly.prompting import PromptInputs, instructions
from toolang.lang.ast import AgicDecl, Program


def instruction_inputs(
    program: Program, agic: AgicDecl, context: Mapping[str, Any]
) -> PromptInputs:
    caps = tuple(
        SimpleNamespace(
            kind=kind,
            effective_ref=item["ref"],
            revision=item["revision"],
            meta={
                "description": item.get("description"),
                **{
                    entry["key"]: entry["value"]
                    for entry in item.get("metadata_items", ())
                },
            },
        )
        for kind, key in (
            ("psyche", "psyches"),
            ("skill", "skills"),
            ("service", "services"),
        )
        for item in context.get(key, ())
    )
    return cast(
        PromptInputs,
        SimpleNamespace(
            program=program,
            agic=agic,
            template_values=context,
            caps=caps,
            psyches={
                item["ref"]: item["content"] for item in context.get("psyches", ())
            },
        ),
    )


def render_instructions(
    program: Program, agic: AgicDecl, context: Mapping[str, Any]
) -> str:
    return instructions(instruction_inputs(program, agic, context))[0]
