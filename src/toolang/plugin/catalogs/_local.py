"""Shared helpers for the built-in local model catalogs."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import logging
import math
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from toolang.base.types.model import (
    CatalogModel,
    CatalogSnapshot,
    CatalogProvider,
)


# A local runtime bills no API tokens. The zero rates are declared on the model's
# `cost` for every meter a probe can report, so accounting stays complete.
LOCAL_ZERO_COST: Mapping[str, int] = {
    "input": 0,
    "output": 0,
    "cache_read": 0,
    "cache_write": 0,
    "reasoning": 0,
    "input_audio": 0,
    "output_audio": 0,
}


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
    if not math.isfinite(value) or value <= 0:
        raise ValueError("local model catalog timeout must be positive and finite")
    return float(value)


def local_snapshot(
    *,
    provider_id: str,
    provider_name: str,
    endpoint: str,
    models: tuple[CatalogModel, ...],
) -> CatalogSnapshot:
    """Build one ephemeral snapshot for a local runtime provider."""

    provider = CatalogProvider(
        id=provider_id,
        name=provider_name,
        adapter="chat_completions",
        api=endpoint,
    )
    identity = json.dumps(
        {
            "provider": provider_id,
            "api": endpoint,
            "models": {model.id: model.to_data() for model in models},
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return CatalogSnapshot(
        providers={provider_id: provider},
        models=tuple(sorted(models, key=lambda model: model.id)),
        revision=f"runtime:{sha256(identity.encode()).hexdigest()}",
        local=True,
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


def resolve_local_endpoint(
    endpoint: str | None,
    *,
    environ: Mapping[str, str],
    env_name: str,
    default_port: int,
) -> str:
    """Resolve one local runtime endpoint from config, environment, or loopback."""

    value = endpoint or environ.get(env_name)
    if value is None:
        host = environ.get("TOOLANG_HOST_GATEWAY", "127.0.0.1")
        return f"http://{host}:{default_port}"
    if "://" not in value:
        value = f"http://{value}"
    if endpoint is None:
        value = replace_guest_loopback(value, environ)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("local model catalog endpoint must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "catalog endpoint must not contain credentials, query, or fragment"
        )
    return value.rstrip("/")


def config_headers(config: Mapping[str, object]) -> Mapping[str, str]:
    """Validate optional discovery headers without publishing credentials."""

    value = config.get("headers", {})
    if not isinstance(value, Mapping) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError("local model catalog headers must map strings to strings")
    return dict(cast(Mapping[str, str], value))


def positive_count(value: object, *, name: str) -> int | None:
    """Normalize discovered counts; leave unknown and sentinel values absent."""

    if type(value) is int and value > 0:
        return value
    if value is not None and (type(value) is not int or value not in {-2, -1, 0}):
        logging.getLogger(__name__).warning("catalog.invalid_count field=%s", name)
    return None


def config_endpoint(config: Mapping[str, object]) -> str | None:
    """Reject an invalid explicit endpoint instead of falling back to localhost."""

    value = config.get("endpoint")
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError("local model catalog endpoint must be a non-empty string")
    return value.strip() if isinstance(value, str) else None
