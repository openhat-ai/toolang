"""Shared terminal-chat session setting and run request operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import cast

from toolang.base.types.policy import RunPolicy
from toolang.execution.policy import apply_session_setting, materialize_run_setting
from toolang.execution.runnables import parse_runnable_ref
from toolang.execution.schemas import RunRequest, RunnableRequest
from toolang.execution.types import RunOverride, SessionSetting
from toolang.lang.input import RunnableInputRaw


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
