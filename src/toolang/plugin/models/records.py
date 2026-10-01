"""Public model and provider inspection records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from toolang.base.types.model import Model, ModelRoute, Provider


def route_record(route: ModelRoute) -> dict[str, object]:
    return {
        "adapter": route.adapter,
        "api": route.api,
        "env": None
        if route.env is None
        else [list(item) if isinstance(item, tuple) else item for item in route.env],
    }


def model_tags(model: Model) -> list[str]:
    route = model._toolang.route
    tags = []
    if not model._toolang.allowed:
        tags.append("not_allowed")
    if route.env is None or route.api_env_missing:
        tags.append("no_env")
    if route.api is None and not route.api_env_missing:
        tags.append("no_api")
    if route.adapter is None:
        tags.append("no_adapter")
    if not tags:
        tags.append("ready")
    tags.append("local" if model._toolang.local else "remote")
    return tags


def _public_data(value: object) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _public_data(item)
            for key, item in value.items()
            if key not in {"headers", "body", "options", "_toolang"}
        }
    if isinstance(value, (tuple, list)):
        return [_public_data(item) for item in value]
    return value


def model_record(model: Model) -> dict[str, Any]:
    data = _public_data(model.to_data())
    data.update(
        ref=model.ref,
        tags=model_tags(model),
        _toolang={
            "provider": model._toolang.provider,
            "route": route_record(model._toolang.route),
        },
    )
    return data


def provider_record(provider: Provider, models: Sequence[Model]) -> dict[str, Any]:
    records = [model_record(model) for model in models]
    ready = sum("ready" in record["tags"] for record in records)
    tags = (
        ["ready"]
        if ready
        else [
            tag
            for tag in ("not_allowed", "no_env", "no_api", "no_adapter")
            if records and all(tag in record["tags"] for record in records)
        ]
    )
    data = provider.to_data()
    data.update(
        models={
            model.id: record for model, record in zip(models, records, strict=True)
        },
        tags=tags,
        _toolang={
            "model_count": len(models),
            "available_models": ready,
            "adapters": list(
                dict.fromkeys(
                    model._toolang.route.adapter
                    for model in models
                    if model._toolang.route.adapter
                )
            ),
            "route": route_record(provider._toolang.route),
        },
    )
    return data
