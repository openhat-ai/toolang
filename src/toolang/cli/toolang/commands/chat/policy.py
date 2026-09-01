"""Shared terminal-chat session setting and run request operations."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from typing import cast

from toolang.base.types.model import ModelRequest
from toolang.base.types.policy import RunPolicy
from toolang.execution.policy import (
    SETTING_OVERRIDE_FORMS,
    apply_session_setting,
    materialize_run_setting,
)
from toolang.execution.runnables import parse_runnable_ref
from toolang.execution.schemas import RunRequest, RunnableRequest
from toolang.execution.types import RunOverride, SessionSetting
from toolang.lang.input import RunnableInputRaw


def setting_slash_usage(name: str) -> str:
    """Return canonical slash usage for one shared setting body."""

    setting_body, _override_bodies = SETTING_OVERRIDE_FORMS[name]
    return f"/{name} {setting_body}"


def run_override_help_lines() -> tuple[str, ...]:
    """Return canonical colon override forms."""

    return tuple(
        f":{name} {body}"
        for name, (_setting_body, override_bodies) in SETTING_OVERRIDE_FORMS.items()
        for body in override_bodies
    )


def run_override_error(source: str, message: str) -> str:
    """Add concise Chat guidance to one rejected colon override."""

    first = source.splitlines()[0].strip()
    if first == ":":
        return "Enter a run override after : · See :? for help"
    head = first.split(maxsplit=1)[0] if first else ""
    name = head.removeprefix(":")
    if name and name not in SETTING_OVERRIDE_FORMS:
        return f"Unknown run override :{name} · See :? for help"
    if "colon override requires runnable input" in message.casefold():
        return "Add runnable input after the override · See :? for help"
    detail = message.rstrip(" .")
    return f"{detail} · See :? for help"


def validate_model_reasoning_request(
    models_payload: Mapping[str, object],
    model: ModelRequest | None,
) -> None:
    """Reject reasoning values known to be unsupported by Chat model metadata."""

    if model is None or model.parameters.reasoning is None:
        return
    reasoning = model.parameters.reasoning
    raw_items = models_payload.get("items")
    items = raw_items if isinstance(raw_items, list | tuple) else ()
    item: Mapping[str, object] | None = None
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        candidate = cast(Mapping[str, object], raw)
        if candidate.get("ref") == model.ref:
            item = candidate
            break
    if item is None:
        return
    raw_parameters = item.get("parameters")
    parameters = (
        cast(Mapping[str, object], raw_parameters)
        if isinstance(raw_parameters, Mapping)
        else {}
    )
    raw_metadata = parameters.get("reasoning")
    metadata = (
        cast(Mapping[str, object], raw_metadata)
        if isinstance(raw_metadata, Mapping)
        else {}
    )
    if reasoning.effort is not None:
        raw_efforts = metadata.get("effort")
        if not isinstance(raw_efforts, list | tuple):
            return
        efforts = tuple(value for value in raw_efforts if isinstance(value, str))
        if reasoning.effort not in efforts:
            joined = ", ".join(efforts) or "none"
            raise ValueError(
                f"model {model.ref} does not advertise reasoning effort "
                f"{reasoning.effort!r} (allowed: {joined})"
            )
        return
    if reasoning.budget_tokens is not None and metadata.get("applicable") is False:
        raise ValueError(
            f"model {model.ref} does not advertise this reasoning token budget"
        )


def build_run_request(
    *,
    thread_id: str,
    request_id: str,
    input: RunnableInputRaw,
    override: RunOverride,
    setting: SessionSetting,
    surface: SessionSetting,
    resolve_model_ref: Callable[[str], str],
    resolve_runnable_ref: Callable[[str], str],
) -> RunRequest:
    """Materialize one session snapshot and input-local override."""

    ceilings, effective = materialize_run_setting(surface, setting, override)
    if effective.runnable is None:
        raise ValueError("chat session has no runnable")
    model = effective.model
    if model is not None:
        model = replace(model, ref=resolve_model_ref(model.ref))
    return RunRequest(
        thread_id=thread_id,
        request_id=request_id,
        runnable=RunnableRequest(
            resolve_runnable_ref(effective.runnable),
            input,
        ),
        model=model,
        policy=RunPolicy(allow=ceilings, limits=effective.limits),
    )


def update_session_setting(
    *,
    surface: SessionSetting,
    current: SessionSetting,
    update: RunOverride,
) -> SessionSetting:
    """Apply one slash setting update without presentation state."""

    return apply_session_setting(surface, current, update)


def reconcile_session_model(
    setting: SessionSetting,
    update: RunOverride,
    *,
    allowed_refs: Collection[str],
) -> SessionSetting:
    """Keep one selected model coherent with the candidate session ceiling."""

    model = setting.model
    if model is None or model.ref in allowed_refs:
        return setting
    if update.model is not None and update.model.identity is not None:
        raise ValueError(f"model is outside session allow.models: {model.ref}")
    if any(item.field == "models" for item in update.allow):
        return replace(setting, model=None)
    raise ValueError(f"session model is outside allow.models: {model.ref}")


def session_model_reconciliation_required(update: RunOverride) -> bool:
    """Return whether one update can change model/ceiling coherence."""

    return (
        update.model is not None and update.model.identity not in {None, "none"}
    ) or any(item.field == "models" for item in update.allow)


def materialize_runnable_list_ref(
    payload: Mapping[str, object],
    selection: str,
    *,
    kind: str | None = None,
) -> str:
    """Resolve one user-facing value to a kind-qualified runnable ref."""

    requested_name, requested_kind = parse_runnable_ref(selection)
    if kind in {"agic", "flow"}:
        if requested_kind not in {None, kind}:
            raise ValueError(f"runnable selection has the wrong kind: {selection}")
        requested_kind = kind
    raw_items = payload.get("items")
    items = raw_items if isinstance(raw_items, list | tuple) else ()
    matches: list[str] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        item = cast(Mapping[str, object], raw)
        name = _text(item.get("name"))
        item_kind = _text(item.get("kind")) or requested_kind
        if name != requested_name or item_kind not in {"agic", "flow"}:
            continue
        if requested_kind is not None and requested_kind != item_kind:
            continue
        matches.append(f"{item_kind}:{name}")
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            f"runnable selection did not match an available route: {selection}"
        )
    joined = ", ".join(matches)
    raise ValueError(f"runnable selection is ambiguous: {selection} (matches {joined})")


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None
