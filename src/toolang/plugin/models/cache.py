"""Secret-free persistent cache for derived model catalog data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import cast

from toolang.base.types.model import ModelCatalogSnapshot, ModelInfo
from toolang.common.files import atomic_write_text, file_write_lock
from toolang.plugin.models.catalog import (
    catalog_json_dumps,
    model_catalog_snapshot_from_data,
)

CACHE_SCHEMA = 1
_CACHE_FILE = "projection.json"
_CACHE_LOCK = ".projection.lock"
_MAX_CACHE_BYTES = 64 * 1024 * 1024
_MAX_CACHED_MODEL_INFOS = 512
_SECRET_FIELD_RE = re.compile(
    r'"(?:api[-_]?key|authorization|cookie|credential|credentials|header|password|'
    r'proxy[-_]?authorization|secret|token|x[-_]?api[-_]?key|[^"\\]*[-_]'
    r'(?:password|secret|token))"\s*:',
    re.IGNORECASE,
)
_HEADER_MAP_RE = re.compile(r'"headers":\{([^{}]*)\}')
_SENSITIVE_HEADER_NAME_RE = re.compile(
    r"(?:authorization|cookie|credential|key|secret|token)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/-]{8,}|https?://[^/\s\"@:]+:[^@\s\"]+@)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    """Cheap identity for one selected file-backed input."""

    path: Path
    device: int
    inode: int
    mtime_ns: int
    size: int

    @classmethod
    def capture(cls, path: Path) -> FileFingerprint:
        """Capture an existing file without reading its contents."""

        resolved = path.expanduser().resolve(strict=True)
        stat = resolved.stat()
        return cls(
            path=resolved,
            device=stat.st_dev,
            inode=stat.st_ino,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
        )

    def to_data(self) -> dict[str, object]:
        """Return a JSON-safe cache identity."""

        return {
            "path": str(self.path),
            "device": self.device,
            "inode": self.inode,
            "mtime_ns": self.mtime_ns,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class CachedModelProjection:
    """Validated cache data reusable by one current catalog source."""

    source: FileFingerprint
    static: ModelCatalogSnapshot
    projection_key: str | None = None
    model_infos: tuple[ModelInfo, ...] | None = None


class ModelProjectionCache:
    """Load and atomically publish one rebuildable model projection."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._path = directory / _CACHE_FILE
        self._lock = directory / _CACHE_LOCK

    def load(
        self,
        source: FileFingerprint,
        *,
        max_source_bytes: int | None = None,
    ) -> CachedModelProjection | None:
        """Return a fully validated matching cache entry, or a cache miss."""

        try:
            if max_source_bytes is not None and source.size > max_source_bytes:
                return None
            if self._path.stat().st_size > _MAX_CACHE_BYTES:
                return None
            payload = json.loads(
                self._path.read_text(encoding="utf-8"),
                parse_float=Decimal,
                parse_constant=_reject_cache_constant,
            )
            if not isinstance(payload, Mapping):
                return None
            if payload.get("schema") != CACHE_SCHEMA:
                return None
            raw_source = payload.get("source")
            if not isinstance(raw_source, Mapping):
                return None
            cached_source = _file_fingerprint_from_data(raw_source)
            if cached_source != source:
                return None
            raw_static = payload.get("static")
            if not isinstance(raw_static, Mapping):
                return None
            revision = raw_static.get("revision")
            data = raw_static.get("data")
            if not isinstance(revision, str) or not revision:
                return None
            static = model_catalog_snapshot_from_data(
                data,
                revision=revision,
                source=source.path,
                catalog="models.dev",
            )
            raw_projection = payload.get("projection")
            if raw_projection is None:
                return CachedModelProjection(source=source, static=static)
            if not isinstance(raw_projection, Mapping):
                return None
            key = raw_projection.get("key")
            raw_infos = raw_projection.get("models")
            if not isinstance(key, str) or not key:
                return None
            if raw_infos is not None and not isinstance(raw_infos, list):
                return None
            infos = None
            if isinstance(raw_infos, list):
                infos = tuple(
                    _model_info_from_cache_data(cast(Mapping[str, object], item))
                    for item in raw_infos
                    if isinstance(item, Mapping)
                )
                if len(infos) != len(raw_infos):
                    return None
            return CachedModelProjection(
                source=source,
                static=static,
                projection_key=key,
                model_infos=infos,
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def store(
        self,
        *,
        source: FileFingerprint,
        static: ModelCatalogSnapshot,
        projection_key: str,
        model_infos: Sequence[ModelInfo],
    ) -> None:
        """Atomically store safe catalog data; unsafe metadata is not cached."""

        infos = (
            [_cache_model_info_data(info) for info in model_infos]
            if len(model_infos) <= _MAX_CACHED_MODEL_INFOS
            else None
        )
        payload = {
            "schema": CACHE_SCHEMA,
            "source": source.to_data(),
            "static": {
                "revision": static.revision,
                "data": static.to_data(),
            },
            "projection": {
                "key": projection_key,
                "models": infos,
            },
        }
        content = _cache_json_dumps(payload)
        if (
            _SECRET_FIELD_RE.search(content)
            or _SECRET_VALUE_RE.search(content)
            or _contains_unsafe_headers(content)
        ):
            return
        with file_write_lock(self._lock):
            atomic_write_text(self._path, content)


def model_projection_key(
    *,
    source: FileFingerprint,
    catalog_revisions: Sequence[tuple[str, str]],
    setup_config: object,
    environment_readiness: Mapping[str, bool],
    adapters: Sequence[str],
) -> str:
    """Build a secret-free digest over every derived-projection input."""

    payload = {
        "schema": CACHE_SCHEMA,
        "source": source.to_data(),
        "catalogs": list(catalog_revisions),
        "setup_config": _canonical_value(setup_config),
        "environment_readiness": dict(sorted(environment_readiness.items())),
        "adapters": sorted(adapters),
    }
    encoded = catalog_json_dumps(payload, indent=None).encode("utf-8")
    return f"sha256:{sha256(encoded).hexdigest()}"


def hydrate_model_infos(
    cached: Sequence[ModelInfo],
    snapshot: ModelCatalogSnapshot,
) -> tuple[ModelInfo, ...] | None:
    """Attach current runtime route facts to a matching cached projection."""

    if len(cached) != len(snapshot.models):
        return None
    hydrated: list[ModelInfo] = []
    for info, model in zip(cached, snapshot.models, strict=True):
        if info.ref != model.identity or model.resolved is None:
            return None
        metadata = dict(info.metadata)
        metadata["resolved_api"] = model.resolved.api
        metadata["resolved_ready"] = model.resolved.ready
        hydrated.append(
            replace(
                info,
                adapter=model.resolved.adapter or "unknown",
                metadata=metadata,
            )
        )
    return tuple(hydrated)


def environment_readiness(
    snapshot: ModelCatalogSnapshot,
    environ: Mapping[str, str],
) -> dict[str, bool]:
    """Return presence facts for catalog-declared environment inputs."""

    names = {name for provider in snapshot.providers.values() for name in provider.env}
    return {name: bool(str(environ.get(name, "")).strip()) for name in sorted(names)}


def _cache_model_info_data(info: ModelInfo) -> dict[str, object]:
    data = info.to_data()
    metadata = data.get("metadata")
    if isinstance(metadata, Mapping):
        data["metadata"] = {
            str(key): value
            for key, value in metadata.items()
            if key not in {"resolved_api", "resolved_ready"}
        }
    data["adapter"] = "unresolved"
    return data


def _model_info_from_cache_data(data: Mapping[str, object]) -> ModelInfo:
    normalized = dict(data)
    for field in ("input_price", "output_price"):
        value = normalized.get(field)
        if isinstance(value, Decimal):
            normalized[field] = float(value)
    return ModelInfo.from_data(normalized)


def _file_fingerprint_from_data(data: Mapping[object, object]) -> FileFingerprint:
    path = data.get("path")
    if not isinstance(path, str) or not path:
        raise TypeError("cached model source path must be text")
    return FileFingerprint(
        path=Path(path),
        device=_required_int(data, "device"),
        inode=_required_int(data, "inode"),
        mtime_ns=_required_int(data, "mtime_ns"),
        size=_required_int(data, "size"),
    )


def _required_int(data: Mapping[object, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"cached model source {key} must be an integer")
    return value


def _cache_json_dumps(value: object) -> str:
    return catalog_json_dumps(value, indent=None)


def _reject_cache_constant(value: str) -> None:
    raise ValueError(f"invalid model cache numeric constant: {value}")


def _contains_unsafe_headers(content: str) -> bool:
    matches = _HEADER_MAP_RE.findall(content)
    if len(matches) != content.count('"headers":'):
        return True
    for raw in matches:
        try:
            headers = json.loads("{" + raw + "}")
        except json.JSONDecodeError:
            return True
        if not isinstance(headers, Mapping) or any(
            not isinstance(name, str)
            or not isinstance(value, str)
            or _SENSITIVE_HEADER_NAME_RE.search(name) is not None
            or _SECRET_VALUE_RE.search(value) is not None
            for name, value in headers.items()
        ):
            return True
    return False


def _canonical_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        return value
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return repr(value)


__all__ = [
    "CachedModelProjection",
    "FileFingerprint",
    "ModelProjectionCache",
    "environment_readiness",
    "hydrate_model_infos",
    "model_projection_key",
]
