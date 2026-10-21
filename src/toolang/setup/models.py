"""Model ordering, configured selection validation, and compaction policy."""

from __future__ import annotations

from collections.abc import Sequence

from toolang.base.model_settings import (
    apply_model_override,
    apply_model_overrides,
    compose_model_overrides,
)
from toolang.base.types.model import Model, ModelOverride, ModelRequest
from toolang.common.errors import ToolangError
from toolang.plugin.models.query import (
    filter_models,
    order_and_allow_models,
    resolve_model,
)
from toolang.plugin.models.resolution import resolve_model_reasoning


def order_models(
    models: Sequence[Model], queries: tuple[str, ...] | None
) -> tuple[tuple[Model, ...], frozenset[str]]:
    """Return every model in catalog/allow order and its allowed refs."""

    return order_and_allow_models(models, queries)


def select_compact_model(
    models: Sequence[Model],
    operations: Sequence[ModelOverride],
    *,
    default: str | None = None,
) -> ModelRequest:
    """Select one allowed, ready tool-call model without building a collection."""

    eligible = tuple(
        model
        for model in filter_models(models, ("*[tool_call]",))
        if model._toolang.effective_ready
    )
    baseline = ModelRequest(default) if default is not None else None
    if any(operation.identity == "unset" for operation in operations):
        raise ToolangError(
            "compact.model requires an exact model or an inherited model"
        )
    request = apply_model_overrides(baseline, operations)
    if request is None:
        raise ToolangError("compaction requires a thread model or compact.model")
    if not any(model.ref == request.ref for model in eligible):
        raise ToolangError(
            f"compact model {request.ref!r} must be available, allowed, and support "
            "tool calls"
        )
    model = resolve_model(eligible, request.ref)
    resolve_model_reasoning(model, request.reasoning)
    return request


def validate_model_defaults(
    models: Sequence[Model],
    *,
    default: ModelRequest | None,
    compact: Sequence[ModelOverride],
    talk: Sequence[ModelOverride],
) -> None:
    """Validate configured choices; defer unknown handler/thread baselines."""

    # Caller baselines are unknown here. Inspect only configured identities;
    # execution still applies the captured operations in their original order.
    compact_override = compose_model_overrides(compact)
    if compact_override is not None and compact_override.identity not in (
        None,
        "default",
    ):
        select_compact_model(models, (compact_override,))
    talk_request = None
    if talk:
        talk_override = compose_model_overrides(talk)
        if default is not None or (
            talk_override is not None
            and talk_override.identity not in (None, "default")
        ):
            talk_request = apply_model_override(default, default, talk_override)
    for request in (talk_request, default):
        if request is not None:
            resolve_model_reasoning(
                resolve_model(models, request.ref), request.reasoning
            )
