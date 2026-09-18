"""Ordering, compact selection, and catalog projection for effective models."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from toolang.base.model_settings import apply_model_override
from toolang.base.types.model import Model, ModelInfo, ModelOverride, ModelRequest
from toolang.common.errors import ToolangError
from toolang.plugin.models.collections import ModelCollection
from toolang.plugin.models.resolution import apply_model_parameters


DEFAULT_PROVIDERS = (
    "alibaba",
    "anthropic",
    "deepseek",
    "google",
    "meta",
    "minimax",
    "mistral",
    "moonshotai",
    "openai",
    "openrouter",
    "xai",
    "zai",
    "zhipuai",
)


def order_models(
    models: ModelCollection, queries: tuple[str, ...] | None
) -> ModelCollection:
    """Authored ordering wins; the fallback ranks providers without excluding any."""
    return models.match(
        queries
        if queries is not None
        else (*[f"{p}/*" for p in DEFAULT_PROVIDERS], "*")
    ).compact()


def select_compact_model(
    models: ModelCollection, override: ModelOverride | None
) -> ModelRequest:
    """Select once from allowed models, independently of the normal Run model."""
    if override is not None and override.identity == "unset":
        raise ToolangError("automatic compaction is disabled by compact.model")
    eligible = models.match("*[tool_call; structured_output]")
    if override is None:
        if not eligible.entries:
            raise ToolangError(
                "compaction requires an allowed model with tool calls and structured output"
            )
        request = ModelRequest(eligible.entries[0].target.ref)
    else:
        request = apply_model_override(None, None, override)
        assert request is not None
        if not eligible.contains(request.ref):
            raise ToolangError(
                f"compact model {request.ref!r} must be available, allowed, and support "
                "tool calls and structured output"
            )
    target = eligible.resolve(request.ref).target
    apply_model_parameters(
        eligible,
        target,
        reasoning=request.reasoning,
        max_output=request.max_output,
    )
    return request


def model_info_from_catalog(
    model: Model,
    *,
    adapter: str | None = None,
    revision: str | None = None,
) -> ModelInfo:
    """Build the one-cycle execution/listing projection for a catalog model."""

    context = model.limit.get("context")
    output = model.limit.get("output")
    input_price = _cost_per_token(model.cost, "input")
    output_price = _cost_per_token(model.cost, "output")
    selectors = [model.id, model.identity, model.name]
    if model.family:
        selectors.append(model.family)
    return ModelInfo(
        ref=model.identity,
        provider=model.provider_id,
        name=model.name,
        model=model.id,
        selectors=tuple(dict.fromkeys(selectors)),
        adapter=adapter
        or (model.resolved.adapter if model.resolved else None)
        or "unknown",
        scope="local" if model.local else "remote",
        tools=model.tool_call is True,
        streaming=True,
        context_window=context,
        max_output_tokens=output,
        input_price=input_price,
        output_price=output_price,
        details=model.description,
        metadata={
            "catalog": model.catalog,
            "catalog_revision": model.catalog_revision or revision,
            "resolved_api": model.resolved.api if model.resolved else None,
            "resolved_ready": model.resolved.ready if model.resolved else False,
            "family": model.family,
            "limit": dict(model.limit),
            "reasoning": model.reasoning,
            "reasoning_options": [
                dict(option) for option in model.reasoning_options or ()
            ],
            "tool_call": model.tool_call,
            "temperature": model.temperature,
            "structured_output": model.structured_output,
            "attachment": model.attachment,
            "open_weights": model.open_weights,
            "release_date": model.release_date,
            "last_updated": model.last_updated,
            "modalities": {key: list(value) for key, value in model.modalities.items()},
            "status": model.status,
            "experimental": (
                dict(model.experimental) if model.experimental is not None else None
            ),
            "cost": dict(model.cost) if model.cost is not None else None,
            "provider": dict(model.provider) if model.provider is not None else None,
            "local": model.local,
        },
    )


def _cost_per_token(cost: Mapping[str, object] | None, key: str) -> float | None:
    if cost is None:
        return None
    value = cost.get(key)
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        return None
    return float(Decimal(value) / Decimal(1_000_000))
