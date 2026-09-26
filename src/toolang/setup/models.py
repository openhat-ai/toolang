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
    models: Sequence[Model], override: ModelOverride | None
) -> ModelRequest:
    """Select one allowed, ready tool-call model without building a collection."""

    if override is not None and override.identity == "unset":
        raise ToolangError("automatic compaction is disabled by compact.model")
    eligible = tuple(
        model
        for model in filter_models(models, ("*[tool_call]",))
        if model._toolang.effective_ready
    )
    if override is None:
        if not eligible:
            raise ToolangError("compaction requires an allowed model with tool calls")
        request = ModelRequest(eligible[0].ref)
    else:
        request = apply_model_override(None, None, override)
        assert request is not None
        if not any(model.ref == request.ref for model in eligible):
            raise ToolangError(
                f"compact model {request.ref!r} must be available, allowed, and support "
                "tool calls"
            )
    model = resolve_model(eligible, request.ref)
    resolve_model_reasoning(model, request.reasoning)
    return request
