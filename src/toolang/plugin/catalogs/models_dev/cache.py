"""Static models.dev catalog artifact cache."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import cast

from toolang.base.types.model import ModelCatalogSnapshot
from toolang.common.cache import (
    CACHE_SCHEMA,
    digest,
    load_document,
    require_fields,
    revision_hex,
    store_document,
)

from .parsing import model_catalog_snapshot_from_data

CATALOG_PARSER_SCHEMA = 1
_CATALOG_FILE = "catalog.json"
_CATALOG_FIELDS = frozenset({"schema", "kind", "key", "source", "snapshot"})


@dataclass(frozen=True, slots=True)
class FileObservation:
    """Process-local identity for one selected file-backed input."""

    path: Path
    device: int
    inode: int
    mtime_ns: int
    size: int

    @classmethod
    def capture(cls, path: Path) -> FileObservation:
        """Capture one existing file without reading its contents."""

        resolved = path.expanduser().resolve(strict=True)
        stat = resolved.stat()
        return cls(
            path=resolved,
            device=stat.st_dev,
            inode=stat.st_ino,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
        )


@dataclass(frozen=True, slots=True)
class CatalogSource:
    """Portable semantic identity of one static catalog source."""

    digest: str
    size: int

    def __post_init__(self) -> None:
        revision_hex(self.digest)
        if self.size < 0:
            raise ValueError("catalog source size must be non-negative")

    @property
    def artifact_key(self) -> str:
        return digest(
            {
                "schema": CACHE_SCHEMA,
                "parser_schema": CATALOG_PARSER_SCHEMA,
                "source": self.to_data(),
            }
        )

    def to_data(self) -> dict[str, object]:
        return {"sha256": self.digest, "size": self.size}


class ModelCatalogArtifactCache:
    """Load and atomically publish one normalized static catalog artifact."""

    def __init__(self, catalog_directory: Path) -> None:
        self._catalog_directory = catalog_directory

    def load_catalog(
        self,
        source: CatalogSource,
        *,
        source_path: Path,
    ) -> ModelCatalogSnapshot | None:
        """Load one validated static catalog artifact, or return a cache miss."""

        path = self._catalog_path(source)
        try:
            document = load_document(
                path,
                kind="catalog",
                key=source.artifact_key,
            )
            require_fields(document, _CATALOG_FIELDS, label="catalog cache")
            raw_source = document.get("source")
            if not isinstance(raw_source, Mapping):
                return None
            cached_source = _catalog_source_from_data(
                cast(Mapping[object, object], raw_source)
            )
            if cached_source != source:
                return None
            raw_snapshot = document.get("snapshot")
            if not isinstance(raw_snapshot, Mapping):
                return None
            snapshot_data = cast(Mapping[str, object], raw_snapshot)
            require_fields(
                snapshot_data,
                frozenset({"revision", "data"}),
                label="catalog snapshot",
            )
            revision = snapshot_data.get("revision")
            data = snapshot_data.get("data")
            if not isinstance(revision, str) or revision != source.digest:
                return None
            return model_catalog_snapshot_from_data(
                data,
                revision=revision,
                source=source_path,
                catalog="models.dev",
            )
        except (OSError, KeyError, TypeError, ValueError):
            return None

    def store_catalog(
        self,
        *,
        source: CatalogSource,
        snapshot: ModelCatalogSnapshot,
    ) -> None:
        """Store one normalized static catalog artifact."""

        if snapshot.revision != source.digest:
            raise ValueError("catalog snapshot revision does not match its source")
        if any(provider.local for provider in snapshot.providers.values()) or any(
            model.local for model in snapshot.models
        ):
            raise ValueError("static catalog artifact cannot contain local models")

        document = {
            "source": source.to_data(),
            "snapshot": {
                "revision": snapshot.revision,
                "data": {
                    provider_id: provider.to_data()
                    for provider_id, provider in sorted(snapshot.providers.items())
                },
            },
        }
        store_document(
            self._catalog_path(source),
            kind="catalog",
            key=source.artifact_key,
            document=document,
        )

    def _catalog_path(self, source: CatalogSource) -> Path:
        revision = revision_hex(source.artifact_key)
        return self._catalog_directory / "catalogs" / "revs" / revision / _CATALOG_FILE


def capture_catalog_source(
    path: Path,
    *,
    max_source_bytes: int | None = None,
    attempts: int = 3,
) -> tuple[FileObservation, CatalogSource]:
    """Capture a stable observation and portable digest for one catalog file."""

    for _ in range(max(attempts, 1)):
        before = FileObservation.capture(path)
        if max_source_bytes is not None and before.size > max_source_bytes:
            raise ValueError(
                f"model catalog exceeds {max_source_bytes} bytes: {before.path}"
            )
        checksum = sha256()
        with before.path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        after = FileObservation.capture(before.path)
        if before == after:
            return before, CatalogSource(
                digest=f"sha256:{checksum.hexdigest()}",
                size=before.size,
            )
    raise RuntimeError(f"model catalog changed while reading: {path}")


def _catalog_source_from_data(data: Mapping[object, object]) -> CatalogSource:
    require_fields(data, frozenset({"sha256", "size"}), label="catalog source")
    value = data.get("sha256")
    size = data.get("size")
    if not isinstance(value, str):
        raise TypeError("cached catalog source digest must be text")
    if isinstance(size, bool) or not isinstance(size, int):
        raise TypeError("cached catalog source size must be an integer")
    return CatalogSource(digest=value, size=size)
