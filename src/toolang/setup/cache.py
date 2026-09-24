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

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

import msgspec

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelRoute,
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
from toolang.common.json import dumps

_SNAPSHOT_DECODER = msgspec.json.Decoder(ModelCatalogSnapshot)

_CATALOG_KIND = "catalog"
_CATALOG_SCHEMA = 2
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


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
        """Persist source declarations; oversized payloads are skipped."""

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
        """Return a content-derived revision independent of cache file timestamps."""

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
            document = load_document(
                path,
                kind=_CATALOG_KIND,
                key=_file_name(name),
                scan_content=False,
            )
            return (
                document if document.get("catalog_schema") == _CATALOG_SCHEMA else None
            )
        except Exception:
            return None

    def _write(self, name: str, document: Mapping[str, object]) -> bool:
        self._directory.mkdir(parents=True, exist_ok=True)
        return store_document(
            self._path(name),
            kind=_CATALOG_KIND,
            key=_file_name(name),
            document={**document, "catalog_schema": _CATALOG_SCHEMA},
            scan_content=False,
        )


def catalog_loader(
    snapshot: ModelCatalogSnapshot, *, revision: str
) -> Callable[[], ModelCatalogSnapshot]:
    """Pin serialized records without retaining a second set of typed indexes.

    Per-source cache files can change or disappear after publication. Decoding
    this private copy preserves the setup version without another source read.
    """

    payload = dumps(
        {**_snapshot_document(snapshot, resolved=True), "revision": revision},
        indent=None,
    )

    def load() -> ModelCatalogSnapshot:
        return _SNAPSHOT_DECODER.decode(payload)

    return load


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
        "catalog_schema": _CATALOG_SCHEMA,
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


def _snapshot_document(
    snapshot: ModelCatalogSnapshot, *, resolved: bool = False
) -> dict[str, object]:
    """Encode source declarations, or explicitly include memory-only setup facts."""

    return {
        "providers": {
            provider_id: _provider_to_data(provider, resolved=resolved)
            for provider_id, provider in sorted(snapshot.providers.items())
        },
        "models": [
            _model_to_data(model, resolved=resolved) for model in snapshot.models
        ],
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
            {
                "schema",
                "catalog_schema",
                "kind",
                "key",
                "revision",
                "providers",
                "models",
                "local",
            }
        ),
        label="model context",
    )
    return _snapshot_from_data(document, revision=revision)


def _snapshot_from_data(
    document: Mapping[str, object], *, revision: str, resolved: bool = False
) -> ModelCatalogSnapshot:
    raw_providers = _mapping(document, "providers")
    raw_models = document["models"]
    if not isinstance(raw_models, list):
        raise TypeError("model context models must be an array")
    if resolved:
        return msgspec.convert(
            {**document, "revision": revision}, type=ModelCatalogSnapshot
        )
    # Disk snapshots carry source declarations, never effective setup routes.
    providers = {
        provider_id: _source_provider(cast(Mapping[str, object], raw))
        for provider_id, raw in raw_providers.items()
    }
    models = []
    for raw in raw_models:
        model = dict(cast(Mapping[str, object], raw))
        toolang = _mapping(model, "_toolang")
        model["_toolang"] = {"provider": toolang["provider"]}
        if model.get("provider") is not None:
            model["provider"] = _source_provider(
                cast(Mapping[str, object], model["provider"])
            )
        models.append(model)
    return msgspec.convert(
        {
            "providers": providers,
            "models": models,
            "revision": revision,
            "local": document.get("local", False),
        },
        type=ModelCatalogSnapshot,
    )


def _source_provider(data: Mapping[str, object]) -> dict[str, object]:
    source = dict(data)
    if source.get("_toolang") is not None:
        declared = dict(_mapping(source, "_toolang"))
        declared.pop("route", None)
        source["_toolang"] = declared
    return source


def _provider_to_data(provider: Provider, *, resolved: bool) -> dict[str, object]:
    return {
        "id": provider.id,
        "name": provider.name,
        "npm": provider.npm,
        "api": provider.api,
        "doc": provider.doc,
        "env": list(provider.env),
        "_toolang": _provider_toolang_to_data(provider._toolang, resolved=resolved),
    }


def _provider_toolang_to_data(
    value: ProviderToolang, *, resolved: bool = False
) -> dict[str, object]:
    return {
        "env": _env_to_data(value.env),
        "adapter": value.adapter,
        **({"route": _route_to_data(value.route)} if resolved else {}),
    }


def _model_to_data(model: Model, *, resolved: bool) -> dict[str, object]:
    data = model.to_data()
    if model.provider is not None and model.provider._toolang is not None:
        data["provider"] = {
            **model.provider.to_data(),
            "_toolang": _provider_toolang_to_data(model.provider._toolang),
        }
    data["_toolang"] = {
        "provider": model._toolang.provider,
        **(
            {
                "ready": model._toolang.ready,
                "route": _route_to_data(model._toolang.route),
            }
            if resolved
            else {}
        ),
    }
    return data


def _route_to_data(route: ModelRoute) -> dict[str, object]:
    return {
        "adapter": route.adapter,
        "api": route.api,
        "env": None if route.env is None else _env_to_data(route.env),
        "headers": dict(route.headers),
        "options": dict(route.options),
    }


def _env_to_data(env: ResolvedEnv) -> list[object]:
    return [
        alternative if isinstance(alternative, str) else list(alternative)
        for alternative in env
    ]


def _mapping(data: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise TypeError(f"model context {name} must be an object")
    return cast(Mapping[str, object], value)


__all__ = [
    "ModelCatalogCache",
    "catalog_loader",
    "environment_identity",
    "model_projection_key",
]
