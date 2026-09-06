"""Model-facing templates for consumed runtime controls."""

from __future__ import annotations

from html import escape

from .records import (
    CancelControlPayload,
    ControlRecord,
    RecallControlPayload,
    SteerControlPayload,
)
from .types import FieldRef, MessageTemplate, RulesRecallTarget, TypedRef


def control_message(control: ControlRecord) -> MessageTemplate | None:
    """Describe a control fact; the caller decides when it is visible."""

    payload = control.payload
    if isinstance(payload, RecallControlPayload):
        target = payload.target
        attrs = (
            {"workspace": target.workspace, "path": target.path}
            if isinstance(target, RulesRecallTarget)
            else {"ref": target.ref}
        )
        attrs["revision"] = payload.revision
        attributes = " ".join(
            f'{key}="{escape(value, quote=True)}"' for key, value in attrs.items()
        )
        return MessageTemplate(
            "user",
            (
                f"<{target.kind} {attributes}>",
                TypedRef(FieldRef.from_path(control.ref, "payload", "content"), "Text"),
                f"</{target.kind}>",
            ),
        )
    if not isinstance(payload, SteerControlPayload | CancelControlPayload):
        return None
    primary = next(
        (
            (index, value)
            for index, value in enumerate(payload.input)
            if value.name == "_"
        ),
        None,
    )
    content = (
        (
            TypedRef(
                FieldRef.from_path(
                    control.ref, "payload", "input", primary[0], "value"
                ),
                primary[1].type,
            ),
        )
        if primary is not None
        else ()
    )
    tag, description = (
        ("steer", "The user supplied updated input for the current task.")
        if isinstance(payload, SteerControlPayload)
        else ("cancel", "The user canceled this run.")
    )
    opening = f'<{tag} description="{description}"'
    return MessageTemplate(
        "user", (opening + ">", *content, f"</{tag}>") if content else (opening + "/>",)
    )
