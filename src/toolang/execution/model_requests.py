"""Read-only provider request projection for normalized model calls."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
import re
from typing import cast

from toolang.base.protocols.model import InspectableModelAdapter
from toolang.base.types.model import ModelTarget
from toolang.base.types.run import ModelCall
from toolang.common.errors import ToolangError
from toolang.plugin.models.config import ProviderConfig
from toolang.plugin.models.resolution import ModelTargetResolver
from toolang.setup import AgentSetup

_SECRET_KEY_RE = re.compile(r"[^a-z0-9]+")
_SECRET_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "clientsecret",
        "password",
        "secret",
    }
)
_HEADER_KEYS = frozenset({"header", "headers"})


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One sanitized provider-native model request body and its target."""

    model: ModelTarget
    body: dict[str, object]


def build_model_request(
    setup: AgentSetup,
    *,
    model_id: str,
    call: ModelCall,
) -> ModelRequest:
    """Project a normalized call through one exact catalog model identity."""

    model = _resolve_exact_model(setup, model_id)
    adapter = setup.adapters.get(model.adapter)
    if adapter is None:
        raise ToolangError(f"unknown model adapter: {model.adapter}")
    if not isinstance(adapter, InspectableModelAdapter):
        raise ToolangError(
            f"model adapter does not support request inspection: {model.adapter}"
        )
    return ModelRequest(
        model=model,
        body=_sanitize_json_object(adapter.request_payload(model, call)),
    )


def _resolve_exact_model(setup: AgentSetup, model_id: str) -> ModelTarget:
    value = model_id.strip()
    provider_id, separator, provider_model_id = value.partition("/")
    if (
        not separator
        or not provider_id
        or not provider_model_id
        or any(character in value for character in "*?[]")
    ):
        raise ToolangError(
            f"model request requires an exact provider/model_id: {model_id}"
        )
    info = next(
        (
            item
            for item in setup.models
            if item.provider == provider_id and item.ref == value
        ),
        None,
    )
    if info is None:
        raise ToolangError(f"unknown model id: {value}")
    ready = info.metadata.get("resolved_ready")
    if ready is False or info.adapter in {"unknown", "unavailable"}:
        raise ToolangError(f"model is unavailable: {value}")
    resolver = ModelTargetResolver(
        providers=setup.providers,
        models=setup.models,
        model_aliases={},
        default_models=(),
        envs=setup.envs,
        provider_configs=cast(
            Mapping[str, ProviderConfig],
            setup.provider_configs,
        ),
    )
    target = resolver.resolve(value)
    if target.ref != value or target.provider != provider_id:
        raise ToolangError(f"model id did not resolve exactly: {value}")
    return target


def _sanitize_json_object(value: Mapping[str, object]) -> dict[str, object]:
    return {
        str(key): _sanitize_json_value(item, key=str(key))
        for key, item in value.items()
    }


def _sanitize_json_value(value: object, *, key: str = "") -> object:
    normalized_key = _SECRET_KEY_RE.sub("", key.lower())
    if normalized_key in _SECRET_KEYS:
        return "<redacted>"
    if normalized_key in _HEADER_KEYS and isinstance(value, Mapping):
        return {str(name): "<redacted>" for name in value}
    if isinstance(value, Mapping):
        return {
            str(name): _sanitize_json_value(item, key=str(name))
            for name, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_sanitize_json_value(item) for item in value]
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ToolangError(
        f"model adapter produced a non-JSON request value: {type(value).__name__}"
    )
