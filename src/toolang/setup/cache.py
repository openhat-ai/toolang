"""Per-catalog model catalog cache.

The setup owns every cache file. Each catalog source persists exactly one
document, in one shared shape: the providers and models that source produced.

Only the reload rule differs. Models.dev re-reads its source file when the
payload digest or the file mtime changed. A local probe rewrites its file only
when the probe result differs, so the file keeps the mtime of the moment the
current run of identical results was first saved — that source's `detected:`
revision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
from typing import cast

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelToolang,
    Provider,
    ProviderToolang,
    ResolvedEnv,
)
from toolang.common.cache import (
    CACHE_SCHEMA,
    canonical_value,
    digest,
    load_document,
    require_fields,
    store_document,
)

_CATALOG_KIND = "catalog"
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_PROVIDER_FIELDS = frozenset(
    {"id", "name", "npm", "api", "doc", "env", "_toolang", "models"}
)
_MODEL_FIELDS = frozenset(
    {
        "id",
        "name",
        "description",
        "family",
        "attachment",
        "reasoning",
        "reasoning_options",
        "tool_call",
        "interleaved",
        "structured_output",
        "temperature",
        "knowledge",
        "release_date",
        "last_updated",
        "modalities",
        "open_weights",
        "limit",
        "status",
        "experimental",
        "provider",
        "cost",
        "extra",
        "_toolang",
    }
)


@dataclass(frozen=True, slots=True)
class CachedCatalog:
    """One catalog source as its plugin produced it, with its own revision."""

    name: str
    revision: str
    snapshot: ModelCatalogSnapshot


class ModelCatalogCache:
    """One cache file per catalog source under one setup cache directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def load_source(
        self,
        name: str,
        *,
        revision: str,
    ) -> ModelCatalogSnapshot | None:
        """Read one source's records, treating drift or damage as a miss."""

        document = self._read(name)
        if document is None or document.get("revision") != revision:
            return None
        try:
            return _snapshot_from_document(document, revision=revision)
        except Exception:
            return None

    def store_source(
        self,
        name: str,
        *,
        revision: str,
        snapshot: ModelCatalogSnapshot,
    ) -> None:
        """Persist one source's records; unsafe or oversized payloads are skipped."""

        self._write(name, {**_snapshot_document(snapshot), "revision": revision})

    def store_probe(self, name: str, *, snapshot: ModelCatalogSnapshot) -> str:
        """Persist one probe result only when it changed; return its stamp."""

        document = _snapshot_document(snapshot)
        content = digest(document)
        path = self._path(name)
        previous = self._read(name)
        if previous is not None and previous.get("content") == content:
            return _detected_revision(path)
        if not self._write(name, {**document, "content": content}):
            return f"probe:{content}"
        return _detected_revision(path)

    def content_revision(self, snapshot: ModelCatalogSnapshot) -> str:
        """Return a content-derived revision for one probe that could not be stored."""

        return f"probe:{digest(_snapshot_document(snapshot))}"

    def probe_revision(self, name: str) -> str | None:
        """Return the stamp one probe file carries, without probing."""

        path = self._path(name)
        return _detected_revision(path) if path.is_file() else None

    def _path(self, name: str) -> Path:
        return self._directory / f"{_file_name(name)}.json"

    def _read(self, name: str) -> dict[str, object] | None:
        path = self._path(name)
        if not path.is_file():
            return None
        try:
            return load_document(
                path,
                kind=_CATALOG_KIND,
                key=_file_name(name),
            )
        except Exception:
            return None

    def _write(self, name: str, document: Mapping[str, object]) -> bool:
        self._directory.mkdir(parents=True, exist_ok=True)
        return store_document(
            self._path(name),
            kind=_CATALOG_KIND,
            key=_file_name(name),
            document=document,
        )


def _file_name(name: str) -> str:
    """Return the cache file stem for one catalog name."""

    if _SAFE_NAME_RE.fullmatch(name):
        return name
    return digest(name).removeprefix("sha256:")


def _detected_revision(path: Path) -> str:
    """Return the `detected:` revision one probe file currently carries."""

    return f"detected:{path.stat().st_mtime_ns}"


def model_projection_key(
    *,
    kind: str,
    scope: str,
    catalog_revisions: Sequence[tuple[str, str]],
    setup_config: object,
    environment: Mapping[str, str],
    plugin_provenance: object,
    allow_models: Sequence[str] | None,
) -> str:
    """Return the content key for one resolved model context."""

    payload = {
        "schema": CACHE_SCHEMA,
        "kind": kind,
        "scope": scope,
        "catalogs": [list(item) for item in catalog_revisions],
        "config": canonical_value(setup_config),
        "environment": {name: value for name, value in sorted(environment.items())},
        "provenance": canonical_value(plugin_provenance),
        "allow": None if allow_models is None else list(allow_models),
    }
    return digest(payload)


def environment_identity(
    environ: Mapping[str, str],
) -> dict[str, str]:
    """Digest the complete environment published in a setup version.

    Tools and API templates may read names not declared by model providers.
    Preserve exact values so a version always identifies the environment it holds.
    """

    return {name: digest(value) for name, value in environ.items()}


# --------------------------------------------------------------------------- #
# codec
# --------------------------------------------------------------------------- #


def _snapshot_document(snapshot: ModelCatalogSnapshot) -> dict[str, object]:
    """Return one catalog's records in the shared cache document shape."""

    return {
        "providers": {
            provider_id: _provider_to_data(provider)
            for provider_id, provider in sorted(snapshot.providers.items())
        },
        "models": [_model_to_data(model) for model in snapshot.models],
        "local": snapshot.local,
    }


def _snapshot_from_document(
    document: Mapping[str, object],
    *,
    revision: str,
) -> ModelCatalogSnapshot:
    require_fields(
        document,
        frozenset(
            {"schema", "kind", "key", "revision", "providers", "models", "local"}
        ),
        label="model context",
    )
    raw_providers = document["providers"]
    raw_models = document["models"]
    if not isinstance(raw_providers, Mapping) or not isinstance(raw_models, list):
        raise TypeError("model context payload must hold providers and models")
    providers = {
        str(provider_id): _provider_from_data(
            str(provider_id),
            cast(Mapping[str, object], raw_provider),
        )
        for provider_id, raw_provider in cast(
            Mapping[object, object], raw_providers
        ).items()
    }
    models = tuple(
        _model_from_data(cast(Mapping[str, object], raw_model))
        for raw_model in raw_models
    )
    return ModelCatalogSnapshot(
        providers=providers,
        models=models,
        revision=revision,
        local=bool(document.get("local", False)),
    )


def _provider_to_data(provider: Provider) -> dict[str, object]:
    return {
        "id": provider.id,
        "name": provider.name,
        "npm": provider.npm,
        "api": provider.api,
        "doc": provider.doc,
        "env": list(provider.env),
        "models": {
            model_id: _model_to_data(model)
            for model_id, model in sorted(provider.models.items())
        },
        "_toolang": _provider_toolang_to_data(provider._toolang),
    }


def _provider_from_data(
    provider_id: str,
    data: Mapping[str, object],
) -> Provider:
    toolang = _mapping(data, "_toolang")
    raw_models = data.get("models")
    if not isinstance(raw_models, Mapping):
        raise TypeError("cached provider models must be an object")
    models = {
        str(model_id): _model_from_data(
            cast(Mapping[str, object], raw_model),
        )
        for model_id, raw_model in cast(Mapping[object, object], raw_models).items()
    }
    return Provider(
        id=provider_id,
        name=_text(data, "name"),
        models=models,
        _toolang=_provider_toolang_from_data(toolang),
        npm=_optional_text(data, "npm"),
        api=_optional_text(data, "api"),
        doc=_optional_text(data, "doc"),
        env=_string_list(data.get("env")),
    )


def _provider_toolang_to_data(value: ProviderToolang) -> dict[str, object]:
    return {
        "env": _env_to_data(value.env),
        "adapter": value.adapter,
    }


def _provider_toolang_from_data(value: object) -> ProviderToolang:
    raw: Mapping[str, object] = (
        cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}
    )
    return ProviderToolang(
        env=_env_from_data(raw.get("env")),
        adapter=_optional_text(raw, "adapter"),
    )


def _model_provider_from_data(value: object) -> Mapping[str, object] | None:
    """Rebuild one model-level corrected provider block."""

    block = _optional_mapping(value)
    if block is None:
        return None
    return {
        str(key): (
            _provider_toolang_from_data(item)
            if key == "_toolang" and isinstance(item, Mapping)
            else item
        )
        for key, item in block.items()
    }


def _model_to_data(model: Model) -> dict[str, object]:
    data = model.to_data()
    if model.provider is not None:
        data["provider"] = {
            str(key): (
                _provider_toolang_to_data(item)
                if isinstance(item, ProviderToolang)
                else item
            )
            for key, item in model.provider.items()
            if key != "_toolang" or isinstance(item, ProviderToolang)
        }
    data["_toolang"] = {
        "ready": model._toolang.ready,
        "provider": model._toolang.provider,
    }
    return data


def _model_from_data(data: Mapping[str, object]) -> Model:
    toolang = _mapping(data, "_toolang")
    provider = _model_provider_from_data(data.get("provider"))
    interleaved = data.get("interleaved")
    return Model(
        id=_text(data, "id"),
        name=_text(data, "name"),
        _toolang=ModelToolang(
            ready=bool(toolang.get("ready", False)),
            provider=_text(toolang, "provider"),
        ),
        description=_optional_text(data, "description"),
        family=_optional_text(data, "family"),
        attachment=_optional_bool(data, "attachment"),
        reasoning=_optional_bool(data, "reasoning"),
        reasoning_options=_optional_mappings(data.get("reasoning_options")),
        tool_call=_optional_bool(data, "tool_call"),
        interleaved=(
            interleaved
            if isinstance(interleaved, bool)
            else _optional_mapping(interleaved)
        ),
        structured_output=_optional_bool(data, "structured_output"),
        temperature=_optional_bool(data, "temperature"),
        knowledge=_optional_text(data, "knowledge"),
        release_date=_optional_text(data, "release_date"),
        last_updated=_optional_text(data, "last_updated"),
        modalities=_modalities(data.get("modalities")),
        open_weights=_optional_bool(data, "open_weights"),
        limit=_int_mapping(data.get("limit")),
        status=_optional_text(data, "status"),
        experimental=_optional_mapping(data.get("experimental")),
        provider=provider,
        cost=_optional_mapping(data.get("cost")),
    )


def _env_to_data(env: ResolvedEnv) -> list[object]:
    return [
        alternative if isinstance(alternative, str) else list(alternative)
        for alternative in env
    ]


def _env_from_data(value: object) -> ResolvedEnv:
    if not isinstance(value, list | tuple):
        return ()
    alternatives: list[str | tuple[str, ...]] = []
    for item in value:
        if isinstance(item, str):
            alternatives.append(item)
        elif isinstance(item, list | tuple):
            alternatives.append(tuple(str(name) for name in item))
    return tuple(alternatives)


def _mapping(data: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise TypeError(f"model context {name} must be an object")
    return cast(Mapping[str, object], value)


def _optional_mapping(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("model context field must be an object")
    return {
        str(key): item for key, item in cast(Mapping[object, object], value).items()
    }


def _optional_mappings(value: object) -> tuple[Mapping[str, object], ...] | None:
    if value is None:
        return None
    if not isinstance(value, list | tuple):
        raise TypeError("model context field must be an array")
    return tuple(_optional_mapping(item) or {} for item in value)


def _modalities(value: object) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("model modalities must be an object")
    return {
        str(key): tuple(str(item) for item in items)
        for key, items in cast(Mapping[object, object], value).items()
        if isinstance(items, list | tuple)
    }


def _int_mapping(value: object) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("model limit must be an object")
    return {
        str(key): int(item)
        for key, item in cast(Mapping[object, object], value).items()
        if isinstance(item, int) and not isinstance(item, bool)
    }


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item) for item in value)


def _text(data: Mapping[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"model context {name} must be text")
    return value


def _optional_text(data: Mapping[str, object], name: str) -> str | None:
    value = data.get(name)
    return value if isinstance(value, str) and value else None


def _optional_bool(data: Mapping[str, object], name: str) -> bool | None:
    value = data.get(name)
    return value if isinstance(value, bool) else None


__all__ = [
    "CachedCatalog",
    "ModelCatalogCache",
    "environment_identity",
    "model_projection_key",
]
