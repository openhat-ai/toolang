"""Inspection projections of canonical setup records."""

from __future__ import annotations

from typing import Any

from toolang.base.types.model import Model, Provider


def _price(value: object) -> str:
    return f"{value:6.2f}" if type(value) in (int, float) else f"{'-':>6}"


def model_record(model: Model) -> dict[str, Any]:
    """Add short inspection fields without changing canonical catalog facts."""

    data = model.to_data()
    cost = model.cost or {}
    data.update(
        tags=list(model._toolang.tags),
        context=model.limit.get("context"),
        max_output=model.limit.get("output"),
        price=f"{_price(cost.get('input'))} /{_price(cost.get('output'))}"
        if any(type(cost.get(key)) in (int, float) for key in ("input", "output"))
        else "-",
        input=list(model.modalities.get("input", ())),
        output=list(model.modalities.get("output", ())),
        features=[
            name
            for name in ("reasoning", "tool_call", "temperature", "structured_output")
            if getattr(model, name) is True
        ],
    )
    return data


def provider_record(provider: Provider) -> dict[str, Any]:
    """Format setup's stored counts and default route without visiting models."""

    data = provider.to_data()
    route = provider._toolang.route.to_data()
    data.update(
        models=f"{provider._toolang.ready_count}/{provider._toolang.model_count}",
        adapter=route["adapter"],
        api=route["api"],
        env=route["env"] or [],
    )
    return data
