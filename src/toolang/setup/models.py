"""Model discovery with a shared, multi-process-safe last-good cache."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
import hashlib
import json
import logging
from pathlib import Path
from time import time

from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelInfo
from toolang.common.files import file_write_lock
from toolang.plugin.models.discovery import (
    default_provider_base_url,
    missing_provider_env_vars,
    required_provider_env_vars,
)

from .records import ModelListRecord

REMOTE_MODEL_LIST_TTL_SEC = 24 * 60 * 60
LOCAL_MODEL_LIST_TTL_SEC = 5.0
_LOGGER = logging.getLogger(__name__)


class ModelListCache:
    """Share provider model lists safely across local processes."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    async def get(
        self,
        provider: ModelProvider,
        *,
        envs: Mapping[str, str],
        refresh: bool = False,
    ) -> tuple[ModelInfo, ...]:
        """Return one provider's current models or its safe last-good list."""

        return await asyncio.to_thread(
            self._get,
            provider,
            dict(envs),
            refresh,
        )

    def read(
        self,
        provider: ModelProvider,
        *,
        envs: Mapping[str, str],
    ) -> ModelListRecord | None:
        """Read one cached provider list without refreshing it."""

        fingerprint = _provider_fingerprint(provider, envs=envs)
        return self._read_path(self._record_path(provider.name, fingerprint))

    def _get(
        self,
        provider: ModelProvider,
        envs: Mapping[str, str],
        refresh: bool,
    ) -> tuple[ModelInfo, ...]:
        fingerprint = _provider_fingerprint(provider, envs=envs)
        path = self._record_path(provider.name, fingerprint)
        lock_path = path.with_suffix(".lock")
        observed = self._read_path(path)
        observed_generation = observed.generation if observed is not None else -1
        local = _is_local_provider(provider, envs=envs)
        ttl_sec = LOCAL_MODEL_LIST_TTL_SEC if local else REMOTE_MODEL_LIST_TTL_SEC
        if (
            not refresh
            and observed is not None
            and _is_fresh(observed, ttl_sec=ttl_sec)
        ):
            return observed.models

        with file_write_lock(lock_path):
            current = self._read_path(path)
            if (
                refresh
                and current is not None
                and current.generation > observed_generation
            ):
                return current.models
            if (
                not refresh
                and current is not None
                and _is_fresh(current, ttl_sec=ttl_sec)
            ):
                return current.models
            try:
                models = tuple(provider.list_models(environ=envs))
            except Exception:
                log = _LOGGER.debug if local else _LOGGER.warning
                log(
                    "model discovery failed provider=%s local=%s",
                    provider.name,
                    local,
                    exc_info=True,
                )
                if not local and current is not None:
                    return current.models
                return ()
            record = ModelListRecord(
                provider=provider.name,
                fingerprint=fingerprint,
                generation=(current.generation + 1 if current is not None else 1),
                fetched_at=time(),
                models=models,
            )
            try:
                record.save(path)
            except (OSError, TypeError, ValueError):
                _LOGGER.warning(
                    "model cache write failed provider=%s path=%s",
                    provider.name,
                    path,
                    exc_info=True,
                )
            return models

    def _record_path(self, provider: str, fingerprint: str) -> Path:
        safe_name = "".join(
            character if character.isalnum() or character in "-_" else "-"
            for character in provider
        ).strip("-")
        return self.directory / f"{safe_name or 'provider'}-{fingerprint[:24]}.json"

    @staticmethod
    def _read_path(path: Path) -> ModelListRecord | None:
        try:
            return ModelListRecord.load(path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None


async def discover_models(
    providers: Mapping[str, ModelProvider],
    *,
    envs: Mapping[str, str],
    cache: ModelListCache,
    refresh: bool = False,
) -> tuple[ModelInfo, ...]:
    """Discover all configured and currently available provider models."""

    available = tuple(
        provider
        for provider in providers.values()
        if provider.name != "custom"
        and not missing_provider_env_vars(provider, environ=envs)
    )
    discovered = await asyncio.gather(
        *(cache.get(provider, envs=envs, refresh=refresh) for provider in available)
    )
    models: dict[tuple[str, str], ModelInfo] = {}
    for provider, provider_models in zip(available, discovered, strict=True):
        for model in provider_models:
            if model.provider != provider.name:
                raise ValueError(
                    f"provider {provider.name!r} returned model for "
                    f"{model.provider!r}: {model.ref}"
                )
            models[(model.provider, model.ref)] = model
    return tuple(models[key] for key in sorted(models))


def _provider_fingerprint(
    provider: ModelProvider,
    *,
    envs: Mapping[str, str],
) -> str:
    required_envs = {
        name: hashlib.sha256(str(envs.get(name, "")).encode()).hexdigest()
        for name in sorted(required_provider_env_vars(provider))
    }
    payload = {
        "class": f"{provider.__class__.__module__}.{provider.__class__.__qualname__}",
        "provider": provider.name,
        "base_url": default_provider_base_url(provider, environ=envs),
        "config": _provider_config(provider),
        "envs": required_envs,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            default=str,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _provider_config(provider: ModelProvider) -> dict[str, object]:
    if is_dataclass(provider) and not isinstance(provider, type):
        names = (field.name for field in fields(provider))
    else:
        names = getattr(provider, "__dict__", {}).keys()
    return {
        name: _safe_config_value(name, getattr(provider, name))
        for name in sorted(names)
        if hasattr(provider, name)
        and name not in {"name", "description"}
        and not name.startswith("_")
        and not any(
            runtime in name.lower()
            for runtime in ("calls", "requests", "responses", "error")
        )
    }


def _safe_config_value(name: str, value: object) -> object:
    lowered = name.lower()
    if lowered.endswith("_env"):
        return str(value)
    if any(word in lowered for word in ("api_key", "token", "secret", "password")):
        return "<redacted>"
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, tuple | list):
        return [_safe_config_value(name, item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _safe_config_value(str(key), item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    return repr(value)


def _is_local_provider(
    provider: ModelProvider,
    *,
    envs: Mapping[str, str],
) -> bool:
    if provider.name == "ollama":
        return True
    base_url = (default_provider_base_url(provider, environ=envs) or "").lower()
    return base_url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))


def _is_fresh(record: ModelListRecord | None, *, ttl_sec: float) -> bool:
    return record is not None and time() - record.fetched_at < ttl_sec
