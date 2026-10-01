"""Immutable model collections with exact lookup and transient TQ selection."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from toolang.base.types.model import Model
from toolang.common.errors import ToolangError
from toolang.common.types import SetOperator

from .query import apply_model_operations, filter_models


@dataclass(frozen=True, slots=True)
class ModelCollection:
    models: tuple[Model, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", tuple(self.models))
        if len({model.ref for model in self.models}) != len(self.models):
            raise ValueError("model collection contains duplicate public refs")

    @property
    def entries(self) -> tuple[Model, ...]:
        return self.models

    def match(self, queries: str | Sequence[str] | None = None) -> ModelCollection:
        values = (queries,) if isinstance(queries, str) else queries
        return ModelCollection(filter_models(self.models, values))

    def apply(
        self, operations: Sequence[tuple[SetOperator, str | Sequence[str]]]
    ) -> ModelCollection:
        return ModelCollection(
            apply_model_operations(
                self.models,
                tuple((op, (q,) if isinstance(q, str) else q) for op, q in operations),
            )
        )

    def resolve(self, ref: str) -> Model:
        for model in self.models:
            if model.ref == ref:
                return model
        raise ToolangError(f"model ref is unavailable: {ref}")

    def entry(self, key: str) -> Model:
        return self.resolve(key)

    def subset(self, keys: Sequence[str]) -> ModelCollection:
        return ModelCollection(tuple(self.resolve(key) for key in keys))

    def compact(self) -> ModelCollection:
        return self

    def contains(self, ref: str) -> bool:
        return any(model.ref == ref for model in self.models)

    def refs(self) -> tuple[str, ...]:
        return tuple(model.ref for model in self.models)

    def keys(self) -> tuple[str, ...]:
        return self.refs()

    def effective_default(self, preferred: str | None) -> str | None:
        return (
            preferred
            if preferred is not None and self.contains(preferred)
            else self.models[0].ref
            if self.models
            else None
        )

    def __len__(self) -> int:
        return len(self.models)
