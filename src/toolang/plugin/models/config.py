"""Model configuration validation.

Provider and model routes live in catalog plugins. Core configuration no longer
carries provider overrides or aliases, so `[models.*]` tables are rejected with
a pointer to the catalog plugin that should own them instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

_REJECTED_MODELS_FIELDS: Mapping[str, str] = {
    "providers": (
        "[models.providers.*] is not supported; declare the provider in a "
        "model catalog plugin"
    ),
    "aliases": (
        "[models.aliases.*] is not supported; declare the model in a model "
        "catalog plugin"
    ),
    "default": (
        "[models].default is not supported; use [default].model with an exact ref"
    ),
}


def validate_models_config(config_layers: Sequence[Mapping[str, object]]) -> None:
    """Reject model configuration that moved into catalog plugins."""

    for payload in config_layers:
        raw = payload.get("models")
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            raise TypeError("models config must be a table")
        for name in raw:
            message = _REJECTED_MODELS_FIELDS.get(str(name))
            if message is not None:
                raise ValueError(message)
            raise ValueError(f"unknown models config field: {name}")
