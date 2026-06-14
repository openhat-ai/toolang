"""Shared model provider discovery helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from time import time
from time import monotonic
from typing import Any
from weakref import WeakKeyDictionary

from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelInfo

_MODEL_INFO_CACHE_TTL_SEC = 30.0
MODEL_INFO_FILE_CACHE_TTL_SEC = 24 * 60 * 60
_MODEL_INFO_FILE_CACHE_VERSION = 1
_PROVIDER_STATE_CACHE_IGNORED_FIELDS = frozenset({"adapter", "options", "scope"})
_MODEL_INFO_CACHE: WeakKeyDictionary[
    ModelProvider,
    dict[tuple[str, str | None, tuple[tuple[str, str], ...]], tuple[float, tuple[ModelInfo, ...]]],
] = WeakKeyDictionary()


def required_provider_env_vars(provider: ModelProvider) -> tuple[str, ...]:
    """Return required environment variables for one provider."""

    try:
        return tuple(var for var in provider.required_env_vars() if str(var).strip())
    except Exception:
        return ()


def missing_provider_env_vars(
    provider: ModelProvider,
    *,
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    """Return missing required environment variables for one provider."""

    return tuple(
        name
        for name in required_provider_env_vars(provider)
        if not str(environ.get(name, "")).strip()
    )


def default_provider_base_url(
    provider: ModelProvider,
    *,
    environ: Mapping[str, str],
) -> str | None:
    """Return the default API base URL for one provider when known."""

    try:
        value = provider.default_base_url(environ=environ)
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def default_provider_api_key_env(provider: ModelProvider) -> str | None:
    """Return the default API key environment variable for one provider when known."""

    try:
        value = provider.default_api_key_env()
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def model_infos(
    provider: ModelProvider,
    *,
    environ: Mapping[str, str],
    cache_dir: Path | None = None,
    refresh: bool = False,
    ttl_sec: float = MODEL_INFO_FILE_CACHE_TTL_SEC,
) -> tuple[ModelInfo, ...]:
    """Return model infos exposed by one provider."""

    cache_bucket = _MODEL_INFO_CACHE.setdefault(provider, {})
    cache_key = _model_info_cache_key(provider, environ=environ)
    now = monotonic()
    cached = None if refresh else cache_bucket.get(cache_key)
    if cached is not None:
        cached_at, infos = cached
        if now - cached_at < _MODEL_INFO_CACHE_TTL_SEC:
            return infos
    if cache_dir is not None and not refresh:
        cached_infos = _load_model_info_file_cache(provider, cache_key=cache_key, cache_dir=cache_dir, ttl_sec=ttl_sec)
        if cached_infos is not None:
            cache_bucket[cache_key] = (now, cached_infos)
            return cached_infos
    try:
        infos = tuple(provider.list_models(environ=environ))
    except Exception:
        return ()
    cache_bucket[cache_key] = (now, infos)
    if cache_dir is not None:
        _write_model_info_file_cache(provider, cache_key=cache_key, cache_dir=cache_dir, infos=infos)
    return infos


def _model_info_cache_key(
    provider: ModelProvider,
    *,
    environ: Mapping[str, str],
) -> tuple[str, str | None, tuple[tuple[str, str], ...]]:
    base_url = default_provider_base_url(provider, environ=environ)
    required = tuple(
        sorted(
            (name, str(environ.get(name, "")))
            for name in required_provider_env_vars(provider)
        )
    )
    return (_provider_cache_identity(provider), base_url, required)


def _provider_cache_identity(provider: ModelProvider) -> str:
    class_name = f"{provider.__class__.__module__}.{provider.__class__.__qualname__}"
    state = _safe_provider_state(getattr(provider, "__dict__", {}))
    state_hash = hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:24]
    return f"{class_name}:{state_hash}"


def _safe_provider_state(value: object) -> object:
    if isinstance(value, Mapping):
        safe: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            field_name = key_text.strip("_").lower()
            if field_name in _PROVIDER_STATE_CACHE_IGNORED_FIELDS:
                continue
            lowered = key_text.lower()
            if any(runtime in lowered for runtime in ("calls", "requests", "responses")):
                continue
            if any(secret in lowered for secret in ("key", "token", "secret", "password")):
                safe[key_text] = "<redacted>"
                continue
            safe[key_text] = _safe_provider_state(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_safe_provider_state(item) for item in value]
    if isinstance(value, ModelInfo):
        return _model_info_to_data(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _load_model_info_file_cache(
    provider: ModelProvider,
    *,
    cache_key: tuple[str, str | None, tuple[tuple[str, str], ...]],
    cache_dir: Path,
    ttl_sec: float,
) -> tuple[ModelInfo, ...] | None:
    path = _model_info_file_cache_path(provider, cache_key=cache_key, cache_dir=cache_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != _MODEL_INFO_FILE_CACHE_VERSION:
        return None
    created_at = payload.get("created_at")
    if not isinstance(created_at, (int, float)) or time() - float(created_at) >= ttl_sec:
        return None
    items = payload.get("models")
    if not isinstance(items, list):
        return None
    models: list[ModelInfo] = []
    for item in items:
        model = _model_info_from_data(item)
        if model is not None:
            models.append(model)
    return tuple(models)


def _write_model_info_file_cache(
    provider: ModelProvider,
    *,
    cache_key: tuple[str, str | None, tuple[tuple[str, str], ...]],
    cache_dir: Path,
    infos: tuple[ModelInfo, ...],
) -> None:
    path = _model_info_file_cache_path(provider, cache_key=cache_key, cache_dir=cache_dir)
    payload = {
        "version": _MODEL_INFO_FILE_CACHE_VERSION,
        "provider": provider.name,
        "created_at": time(),
        "models": [_model_info_to_data(info) for info in infos],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except (OSError, TypeError):
        return


def _model_info_file_cache_path(
    provider: ModelProvider,
    *,
    cache_key: tuple[str, str | None, tuple[tuple[str, str], ...]],
    cache_dir: Path,
) -> Path:
    key_hash = hashlib.sha256(json.dumps(cache_key, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
    return cache_dir / f"{provider.name}-{key_hash}.json"


def _model_info_to_data(info: ModelInfo) -> dict[str, Any]:
    data = asdict(info)
    data["selectors"] = list(info.selectors)
    data["tags"] = list(info.tags)
    return data


def _model_info_from_data(data: object) -> ModelInfo | None:
    if not isinstance(data, dict):
        return None
    mapping = {str(key): value for key, value in data.items()}
    try:
        return ModelInfo(
            ref=str(mapping["ref"]),
            provider=str(mapping["provider"]),
            name=str(mapping["name"]),
            model=str(mapping["model"]),
            selectors=tuple(str(item) for item in _list(mapping.get("selectors"))),
            adapter=str(mapping.get("adapter") or "default"),
            scope=str(mapping["scope"]) if mapping.get("scope") is not None else None,
            tags=tuple(str(item) for item in _list(mapping.get("tags"))),
            tools=bool(mapping.get("tools", True)),
            streaming=bool(mapping.get("streaming", True)),
            context_window=_int_or_none(mapping.get("context_window")),
            max_output_tokens=_int_or_none(mapping.get("max_output_tokens")),
            input_price=_float_or_none(mapping.get("input_price")),
            output_price=_float_or_none(mapping.get("output_price")),
            details=str(mapping["details"]) if mapping.get("details") is not None else None,
            metadata=_dict_or_empty(mapping.get("metadata")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _dict_or_empty(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    return None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
