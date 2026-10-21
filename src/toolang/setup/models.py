"""Model ordering, transient query selection, and compact-model resolution."""

from __future__ import annotations

from collections.abc import Sequence

from toolang.base.model_settings import apply_model_override
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
    request = baseline
    for operation in operations:
        if operation.identity == "unset":
            raise ToolangError(
                "compact.model requires an exact model or an inherited model"
            )
        request = apply_model_override(request, baseline, operation)
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
