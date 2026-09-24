"""Project a scheduled Run's durable outcome into caller context."""

from collections.abc import Callable
from dataclasses import replace
from html import escape
import json

from toolang.base.types.message import (
    Part,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)

from ..records import RunRecord, StepRecord
from ..types import (
    ControlRef,
    ErrorMessage,
    ErrorRef,
    FieldRef,
    MessageTemplate,
    RunRef,
    ToolStepGiven,
    TypedRef,
)
from ..values import parts_from_local


def run_receipt(run_id: str) -> dict[str, object]:
    """Acknowledge acceptance, independently of the target's eventual outcome."""

    return {"run_id": run_id, "controls": [str(ControlRef.for_run(run_id, 0))]}


def scheduled_run(step: StepRecord) -> str | None:
    """Recognize committed scheduling receipts, excluding legacy result replies."""

    if not (
        isinstance(step.given, ToolStepGiven)
        and step.given.trigger == "model"
        and step.given.call.name == "_toolang__run"
        and step.output is not None
        and isinstance(step.output.local.value, ToolResultPart)
    ):
        return None
    return scheduled_run_id(step.output.local.value)


def scheduled_run_id(receipt: ToolResultPart) -> str | None:
    """Read the target identity from an accepted scheduling receipt."""

    run_id = receipt.output.get("run_id")
    if (
        receipt.error is None
        and isinstance(run_id, str)
        and receipt.output.get("controls") == [str(ControlRef.for_run(run_id, 0))]
    ):
        return run_id
    return None


def run_completion(
    run: RunRecord,
    resolve: Callable[[object], object],
    resolve_error: Callable[[ErrorMessage | ErrorRef], str],
) -> MessageTemplate:
    """Use the same terminal facts online and when reconstructing a history tail."""

    if run.status in {"pending", "running"}:
        raise ValueError(f"Run has no completion: {run.id}")
    attributes = f'run="{escape(run.id, quote=True)}" status="{run.status}"'
    content = ()
    if run.status == "succeeded" and run.output is not None:
        value = run.output.local
        resolved = replace(value, value=resolve(value.value))
        raw_parts = parts_from_local(resolved)
        parts = parts_from_local(resolved, content_only=True)
        attributes += f' output-type="{escape(value.type, quote=True)}"'
        if value.type in {"Text", "Part", "Part[]"} and not any(
            isinstance(part, ReasoningPart | ToolCallPart | ToolResultPart)
            or (
                isinstance(part, TextPart)
                and (
                    part.signature is not None
                    or part.provider is not None
                    or bool(part.provider_metadata)
                )
            )
            for part in raw_parts
        ):
            content = (
                TypedRef(
                    FieldRef.from_path(RunRef(run.id), "output", "local", "value"),
                    value.type,
                ),
            )
        else:
            content = tuple(_context_part(part) for part in parts)
    elif run.error is not None:
        content = (escape(resolve_error(run.error), quote=False),)
    return MessageTemplate(
        "user",
        (f"<toolang:run-result {attributes}>", *content, "</toolang:run-result>"),
        tag="run-result",
        escape_text=True,
    )


def _context_part(part: Part) -> Part:
    """Return control-shaped output as data, never as a caller tool exchange."""

    if isinstance(part, ToolCallPart | ToolResultPart):
        part = TextPart(
            json.dumps(part.to_data(), ensure_ascii=False, separators=(",", ":"))
        )
    return (
        TextPart(escape(part.text, quote=False)) if isinstance(part, TextPart) else part
    )
