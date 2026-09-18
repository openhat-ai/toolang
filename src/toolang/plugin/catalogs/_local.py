"""Shared helpers for the built-in local model catalogs."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from toolang.base.types.model import Model, ModelCatalogSnapshot, Provider


def optional_string(value: object) -> str | None:
    """Return stripped text, or ``None`` for an absent or empty value."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def positive_int(value: object) -> int | None:
    """Return a positive integer, or ``None`` for any other value."""

    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def mapping(value: object) -> dict[str, object]:
    """Return one decoded object with string keys, or an empty mapping."""

    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def compact_mapping(values: Mapping[str, object | None]) -> dict[str, object]:
    """Drop ``None`` values from one mapping."""

    return {key: value for key, value in values.items() if value is not None}


def model_entries(items: list[object]) -> tuple[tuple[str, dict[str, object]], ...]:
    """Return unique, sorted ``(model_id, entry)`` pairs from a runtime listing."""

    entries: dict[str, dict[str, object]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        data = cast(dict[str, object], item)
        raw = data.get("model") or data.get("name") or data.get("id")
        if isinstance(raw, str) and raw.strip():
            entries.setdefault(raw.strip(), data)
    return tuple(sorted(entries.items()))


def config_environ(config: Mapping[str, object]) -> Mapping[str, str]:
    """Read the injected process environment subset for one local catalog."""

    value = config.get("environ")
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def config_timeout(config: Mapping[str, object]) -> float:
    """Read and validate one local catalog probe timeout."""

    value = config.get("timeout", 2.0)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("local model catalog timeout must be numeric")
    return float(value)


def local_snapshot(
    *,
    provider_id: str,
    provider_name: str,
    endpoint: str,
    models: tuple[Model, ...],
    provider_runtime: Mapping[str, object],
) -> ModelCatalogSnapshot:
    """Build one ephemeral snapshot for a local runtime provider."""

    by_id = {model.id: model for model in models}
    provider = Provider(
        id=provider_id,
        name=provider_name,
        env=(),
        npm="@ai-sdk/openai-compatible",
        api=endpoint,
        models=by_id,
        extra={"runtime": dict(provider_runtime)},
        local=True,
    )
    identity = json.dumps(
        provider.to_data(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ModelCatalogSnapshot(
        providers={provider_id: provider},
        models=tuple(by_id[key] for key in sorted(by_id)),
        revision=f"runtime:{sha256(identity.encode()).hexdigest()}",
    )


def replace_guest_loopback(value: str, environ: Mapping[str, str]) -> str:
    """Rewrite a default loopback endpoint to the Toolang host gateway."""

    gateway = environ.get("TOOLANG_HOST_GATEWAY")
    if not gateway:
        return value
    try:
        parsed = urlsplit(value)
        if parsed.hostname not in {
            "0.0.0.0",
            "127.0.0.1",
            "localhost",
            "::",
            "::1",
        }:
            return value
        port = parsed.port
    except ValueError:
        return value
    gateway_host = f"[{gateway}]" if ":" in gateway else gateway
    netloc = f"{gateway_host}:{port}" if port is not None else gateway_host
    return urlunsplit(parsed._replace(netloc=netloc))
