"""Models.dev-compatible file-backed model catalog plugin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import msgspec

from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import ModelCatalogSnapshot

from .parsing import model_catalog_snapshot_from_data
from .path import DEFAULT_MAX_CATALOG_BYTES


@dataclass(frozen=True, slots=True)
class ModelsDevModelCatalog(ModelCatalog):
    """One complete models.dev-compatible file-backed catalog."""

    path: Path
    max_bytes: int = DEFAULT_MAX_CATALOG_BYTES
    name: str = "models_dev"

    async def snapshot(self) -> ModelCatalogSnapshot:
        """Load and validate the selected catalog file."""

        return read_model_catalog_snapshot(self.path, max_bytes=self.max_bytes)

    def capture(self) -> tuple[FileObservation, ModelCatalogSource]:
        """Read the selected file once for change detection and later parsing."""

        return capture_model_catalog_source(self.path, max_bytes=self.max_bytes)


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
class ModelCatalogSource:
    """One stable read of a static catalog file with its portable revision."""

    path: Path
    payload: bytes
    revision: str

    def snapshot(self) -> ModelCatalogSnapshot:
        """Validate the captured payload and rebuild its snapshot."""

        return _model_catalog_snapshot_from_bytes(
            self.payload,
            source=self.path,
            revision=self.revision,
        )


def create_model_catalog(config: Mapping[str, object]) -> ModelCatalog:
    """Create the built-in models.dev file catalog plugin."""

    value = config.get("path")
    if not isinstance(value, str | Path):
        raise ValueError("models_dev catalog requires path")
    max_bytes = config.get("max_bytes", DEFAULT_MAX_CATALOG_BYTES)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("models_dev catalog max_bytes must be an integer")
    return ModelsDevModelCatalog(Path(value), max_bytes=max_bytes)


def read_model_catalog_snapshot(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_CATALOG_BYTES,
) -> ModelCatalogSnapshot:
    """Load one complete validated models.dev provider or combined catalog."""

    _, source = capture_model_catalog_source(path, max_bytes=max_bytes)
    return source.snapshot()


def capture_model_catalog_source(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_CATALOG_BYTES,
    attempts: int = 3,
) -> tuple[FileObservation, ModelCatalogSource]:
    """Read one catalog file with a stable observation and portable revision."""

    for _ in range(max(attempts, 1)):
        before = FileObservation.capture(path)
        if before.size > max_bytes:
            raise ValueError(f"model catalog exceeds {max_bytes} bytes: {before.path}")
        payload = before.path.read_bytes()
        after = FileObservation.capture(before.path)
        if before == after:
            return before, ModelCatalogSource(
                path=before.path,
                payload=payload,
                # The payload digest and the mtime are both part of the revision:
                # touching the file reloads it even when its content is unchanged.
                revision=(
                    f"sha256:{sha256(payload).hexdigest()}+mtime:{after.mtime_ns}"
                ),
            )
    raise RuntimeError(f"model catalog changed while reading: {path}")


def _model_catalog_snapshot_from_bytes(
    payload_bytes: bytes,
    *,
    source: Path | None,
    revision: str,
) -> ModelCatalogSnapshot:
    """Validate one complete models.dev payload and rebuild its snapshot."""

    try:
        payload = msgspec.json.decode(payload_bytes)
    except msgspec.DecodeError as exc:
        raise ValueError(f"invalid model catalog JSON: {source}: {exc}") from exc
    return model_catalog_snapshot_from_data(
        payload,
        revision=revision,
        source=source,
    )
