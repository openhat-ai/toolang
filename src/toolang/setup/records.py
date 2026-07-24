"""Persisted model-list cache records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from typing import cast

from toolang.base.types.model import ModelInfo
from toolang.common.files import atomic_write_text

MODEL_LIST_RECORD_VERSION = 1


@dataclass(frozen=True, slots=True)
class ModelListRecord:
    """One last-good model list for a provider configuration."""

    provider: str
    fingerprint: str
    generation: int
    fetched_at: float
    models: tuple[ModelInfo, ...]
    version: int = MODEL_LIST_RECORD_VERSION

    @classmethod
    def load(cls, path: Path) -> ModelListRecord:
        """Load one model-list record."""

        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise TypeError("model-list record must be an object")
        version = payload.get("version")
        if version != MODEL_LIST_RECORD_VERSION:
            raise ValueError(f"unsupported model-list record version: {version!r}")
        models = payload.get("models")
        if not isinstance(models, list):
            raise TypeError("model-list record models must be a list")
        if not all(isinstance(item, Mapping) for item in models):
            raise TypeError("model-list record models must contain objects")
        generation = payload.get("generation")
        fetched_at = payload.get("fetched_at")
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise TypeError("model-list generation must be an integer")
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, int | float):
            raise TypeError("model-list fetched_at must be a number")
        return cls(
            provider=str(payload["provider"]),
            fingerprint=str(payload["fingerprint"]),
            generation=generation,
            fetched_at=float(fetched_at),
            models=tuple(
                ModelInfo.from_data(cast(Mapping[str, object], item))
                for item in models
            ),
        )

    def save(self, path: Path) -> None:
        """Atomically save this model-list record."""

        atomic_write_text(
            path,
            json.dumps(
                {
                    "version": self.version,
                    "provider": self.provider,
                    "fingerprint": self.fingerprint,
                    "generation": self.generation,
                    "fetched_at": self.fetched_at,
                    "models": [model.to_data() for model in self.models],
                },
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
