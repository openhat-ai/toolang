"""Models.dev-compatible file-backed model catalog plugin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

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


def create_models_dev_model_catalog(config: Mapping[str, object]) -> ModelCatalog:
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

    resolved = path.expanduser().resolve(strict=True)
    payload_bytes = resolved.read_bytes()
    if len(payload_bytes) > max_bytes:
        raise ValueError(f"model catalog exceeds {max_bytes} bytes: {resolved}")
    try:
        payload = json.loads(
            payload_bytes,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid model catalog JSON: {resolved}: {exc}") from exc
    revision = f"sha256:{sha256(payload_bytes).hexdigest()}"
    return model_catalog_snapshot_from_data(
        payload,
        revision=revision,
        source=resolved,
        catalog="models.dev",
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant: {value}")
