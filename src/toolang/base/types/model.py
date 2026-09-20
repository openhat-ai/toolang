"""Shared model catalog and execution value types."""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Self, TypeAlias

from pydantic import ConfigDict


def _immutable_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {str(key): _immutable_json(item) for key, item in value.items()}
    )


def _immutable_json(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("catalog numbers must be finite")
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _immutable_json(item) for key, item in value.items()}
        )
    if isinstance(value, tuple | list):
        return tuple(_immutable_json(item) for item in value)
    return value


ResolvedEnv = tuple[str | tuple[str, ...], ...]
# Effect levels are provider-defined; the catalog's reasoning_options is the only
# source of truth, so Toolang keeps no closed vocabulary here.
ModelEffort: TypeAlias = str | int | Literal["auto"]
ModelMaxOutput: TypeAlias = int | Literal["auto"]


def _exclude_none(value: object) -> bool:
    return value is None


@dataclass(frozen=True, slots=True)
class Reasoning:
    """One reasoning control requested for a model selection or call."""

    effort: str | None = field(
        default=None,
        metadata={"exclude_if": _exclude_none},
    )
    budget_tokens: int | None = field(
        default=None,
        metadata={"exclude_if": _exclude_none, "strict": True},
    )

    def __post_init__(self) -> None:
        if self.effort is not None and not (
            isinstance(self.effort, str) and self.effort.strip()
        ):
            raise ValueError("reasoning effort requires a non-empty level")
        if self.budget_tokens is not None:
            if isinstance(self.budget_tokens, bool) or not isinstance(
                self.budget_tokens, int
            ):
                raise TypeError("reasoning budget_tokens must be an integer")
            if self.budget_tokens < 0:
                raise ValueError("reasoning budget_tokens must be non-negative")
        if self.effort is not None and self.budget_tokens is not None:
            raise ValueError("reasoning accepts either effort or budget_tokens")

    def to_data(self) -> dict[str, object]:
        """Return the reasoning control as a protocol-neutral mapping."""

        data: dict[str, object] = {}
        if self.effort is not None:
            data["effort"] = self.effort
        if self.budget_tokens is not None:
            data["budget_tokens"] = self.budget_tokens
        return data


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One exact model ref and the controls this run asks for."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    ref: str
    reasoning: Reasoning | None = None
    max_output: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ref, str):
            raise TypeError("model request ref must be a string")
        if not self.ref or self.ref != self.ref.strip():
            raise ValueError("model request requires a canonical ref")
        if (
            self.ref.startswith("/")
            or self.ref.endswith("/")
            or any(character.isspace() for character in self.ref)
            or any(character in self.ref for character in '*?[],;"')
        ):
            raise ValueError(f"model request ref must be exact: {self.ref!r}")
        if self.reasoning is not None and not isinstance(self.reasoning, Reasoning):
            raise TypeError("model request reasoning must be Reasoning")
        if self.max_output is not None and (
            isinstance(self.max_output, bool)
            or not isinstance(self.max_output, int)
            or self.max_output <= 0
        ):
            raise ValueError(
                "model request max_output must be a positive integer or none"
            )


@dataclass(frozen=True, slots=True)
class ModelOverride:
    """Sparse model identity and typed parameter changes from one input source."""

    identity: str | None = None
    effort: ModelEffort | None = None
    max_output: ModelMaxOutput | None = None

    def __post_init__(self) -> None:
        if self.identity is not None:
            if self.identity == "none":
                raise ValueError(
                    "model identity 'none' was removed; use 'unset' for a "
                    "model-free selection"
                )
            if self.identity not in {"default", "unset"}:
                ModelRequest(self.identity)
        if self.effort is not None:
            if isinstance(self.effort, bool):
                raise TypeError("model effort must be a level, token budget, or auto")
            if isinstance(self.effort, int):
                if self.effort < 0:
                    raise ValueError("model effort token budget must be non-negative")
            elif not self.effort.strip():
                raise ValueError("model effort requires a non-empty level or auto")
        if self.max_output is not None and self.max_output != "auto":
            if isinstance(self.max_output, bool) or not isinstance(
                self.max_output, int
            ):
                raise TypeError("model max_output must be a token count or auto")
            if self.max_output <= 0:
                raise ValueError("model max_output tokens must be positive")
        if self.identity is None and self.effort is None and self.max_output is None:
            raise ValueError(
                "model override requires an identity, effort, or max_output"
            )
        if self.identity == "unset" and (
            self.effort is not None or self.max_output is not None
        ):
            raise ValueError("model unset cannot combine with parameters")


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """Immutable effective connection published by setup, never a source record."""

    adapter: str | None = None
    api: str | None = None
    env: ResolvedEnv | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "options", _immutable_mapping(self.options))
        if self.env is not None:
            object.__setattr__(self, "env", normalized_env(self.env))

    @property
    def ready(self) -> bool:
        return (
            self.adapter is not None and self.api is not None and self.env is not None
        )


@dataclass(frozen=True, slots=True)
class ProviderToolang:
    """Trusted catalog declarations and setup's effective default route."""

    env: ResolvedEnv = ()
    adapter: str | None = None
    route: ModelRoute = field(default_factory=ModelRoute)


@dataclass(frozen=True, slots=True)
class ModelToolang:
    """Ownership and effective connection facts published by setup."""

    ready: bool = False
    provider: str = ""
    route: ModelRoute = field(default_factory=ModelRoute)


@dataclass(frozen=True, slots=True)
class ModelProvider:
    """Catalog connection overrides for a model's owning provider."""

    npm: str | None = None
    api: str | None = None
    shape: str | None = None
    mode: str | None = None
    headers: Mapping[str, str] | None = None
    body: Mapping[str, object] | None = None
    _toolang: ProviderToolang | None = None

    def __post_init__(self) -> None:
        if self._toolang is not None and not isinstance(self._toolang, ProviderToolang):
            raise TypeError("model provider _toolang must be ProviderToolang")
        if self.headers is not None:
            object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        if self.body is not None:
            object.__setattr__(self, "body", _immutable_mapping(self.body))

    def to_data(self) -> dict[str, object]:
        """Return public catalog declarations without trusted Toolang metadata."""

        return {
            name: _mutable_json(value)
            for name in ("npm", "api", "shape", "mode", "headers", "body")
            if (value := getattr(self, name)) is not None
        }


@dataclass(frozen=True, slots=True)
class Model:
    """One models.dev-compatible model record within a provider."""

    id: str
    name: str
    _toolang: ModelToolang
    description: str | None = None
    family: str | None = None
    attachment: bool | None = None
    reasoning: bool | None = None
    reasoning_options: tuple[Mapping[str, object], ...] | None = None
    tool_call: bool | None = None
    interleaved: bool | Mapping[str, object] | None = None
    structured_output: bool | None = None
    temperature: bool | None = None
    knowledge: str | None = None
    release_date: str | None = None
    last_updated: str | None = None
    modalities: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    open_weights: bool | None = None
    limit: Mapping[str, int] = field(default_factory=dict)
    status: str | None = None
    experimental: Mapping[str, object] | None = None
    # The corrected provider this model must use, not the owning provider;
    # `_toolang.provider` carries the association.
    provider: ModelProvider | None = None
    cost: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ValueError("model id and name are required")
        if self.provider is not None and not isinstance(self.provider, ModelProvider):
            raise TypeError("model provider must be ModelProvider")
        if not self._toolang.provider:
            raise ValueError("model requires its provider id")
        # A read-only view may still wrap a dictionary owned by a plugin.
        # Detach all nested values before publishing or resolving a record.
        object.__setattr__(
            self,
            "modalities",
            MappingProxyType(
                {str(key): tuple(value) for key, value in self.modalities.items()}
            ),
        )
        object.__setattr__(self, "limit", MappingProxyType(dict(self.limit)))
        if self.reasoning_options is not None:
            object.__setattr__(
                self,
                "reasoning_options",
                tuple(_immutable_mapping(option) for option in self.reasoning_options),
            )
        for name in ("experimental", "cost"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _immutable_mapping(value))
        if isinstance(self.interleaved, Mapping):
            object.__setattr__(
                self, "interleaved", _immutable_mapping(self.interleaved)
            )

    def with_route(self, route: ModelRoute) -> Self:
        """Publish a route while sharing this record's already detached catalog facts."""

        result = copy(self)
        object.__setattr__(
            result,
            "_toolang",
            ModelToolang(
                ready=route.ready, provider=self._toolang.provider, route=route
            ),
        )
        return result

    @property
    def identity(self) -> str:
        """Return the exact provider/model catalog identity."""

        return f"{self._toolang.provider}/{self.id}"

    @property
    def ref(self) -> str:
        """Return the public exact ref used to select this model."""

        return self.identity

    def to_data(self) -> dict[str, object]:
        """Return this model in models.dev-compatible JSON form."""

        data: dict[str, object] = {}
        data.update(
            {
                "id": self.id,
                "name": self.name,
                "attachment": self.attachment,
                "reasoning": self.reasoning,
                "tool_call": self.tool_call,
                "structured_output": self.structured_output,
                "temperature": self.temperature,
                "modalities": {
                    key: list(value) for key, value in self.modalities.items()
                },
                "open_weights": self.open_weights,
                "limit": dict(self.limit),
            }
        )
        optional = {
            "description": self.description,
            "family": self.family,
            "reasoning_options": self.reasoning_options,
            "interleaved": self.interleaved,
            "knowledge": self.knowledge,
            "release_date": self.release_date,
            "last_updated": self.last_updated,
            "status": self.status,
            "experimental": self.experimental,
            "provider": self.provider.to_data() if self.provider is not None else None,
            "cost": self.cost,
        }
        data.update({key: _mutable_json(value) for key, value in optional.items()})
        return {key: value for key, value in data.items() if value is not None}


def normalized_env(env: ResolvedEnv) -> ResolvedEnv:
    """Normalize one provider environment rule into OR-of-AND form."""

    normalized: list[str | tuple[str, ...]] = []
    for alternative in env:
        if isinstance(alternative, str):
            name = alternative.strip()
            if not name:
                raise ValueError("provider env names must be non-empty")
            normalized.append(name)
            continue
        group = tuple(name.strip() for name in alternative if name.strip())
        if not group:
            raise ValueError("provider env groups must be non-empty")
        normalized.append(group[0] if len(group) == 1 else group)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class Provider:
    """One provider definition; models link to it through their ownership key."""

    id: str
    name: str
    _toolang: ProviderToolang = ProviderToolang()
    npm: str | None = None
    api: str | None = None
    doc: str | None = None
    env: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ValueError("provider id and name are required")

    def to_data(
        self, *, models: Mapping[str, Model] | None = None
    ) -> dict[str, object]:
        """Return this provider in models.dev-compatible JSON form."""

        data: dict[str, object] = {}
        data.update(
            {
                "id": self.id,
                "name": self.name,
                "env": list(self.env),
                "models": {
                    key: model.to_data()
                    for key, model in sorted((models or {}).items())
                },
            }
        )
        if self.npm is not None:
            data["npm"] = self.npm
        if self.api is not None:
            data["api"] = self.api
        if self.doc is not None:
            data["doc"] = self.doc
        return data


def env_names(env: ResolvedEnv) -> tuple[str, ...]:
    """Return every environment name one rule mentions, in order."""

    return tuple(
        name
        for alternative in env
        for name in ((alternative,) if isinstance(alternative, str) else alternative)
    )


@dataclass(frozen=True, slots=True)
class ModelCatalogSnapshot:
    """One immutable model catalog and availability snapshot."""

    providers: Mapping[str, Provider]
    models: tuple[Model, ...]
    revision: str
    source: Path | None = None
    local: bool = False

    def __post_init__(self) -> None:
        providers = dict(self.providers)
        models = tuple(self.models)
        if any(key != provider.id for key, provider in providers.items()):
            raise ValueError("catalog provider keys must match provider ids")
        identities = [(model._toolang.provider, model.id) for model in models]
        if len(identities) != len(set(identities)):
            raise ValueError("catalog models must have unique provider/model identity")
        if any(model._toolang.provider not in providers for model in models):
            raise ValueError("catalog models reference unknown providers")
        object.__setattr__(self, "providers", MappingProxyType(providers))
        object.__setattr__(self, "models", models)

    def find(self, provider_id: str, model_id: str) -> Model | None:
        """Find one exact model in this snapshot."""

        return next(
            (
                model
                for model in self.models
                if model._toolang.provider == provider_id and model.id == model_id
            ),
            None,
        )

    def to_data(
        self,
        *,
        models: tuple[Model, ...] | None = None,
    ) -> dict[str, object]:
        """Return a complete models.dev-compatible provider map."""

        selected = self.models if models is None else models
        by_provider: dict[str, dict[str, Model]] = {}
        if self.local:
            raise ValueError("a local-only catalog cannot be exported")
        for model in selected:
            by_provider.setdefault(model._toolang.provider, {})[model.id] = model
        return {
            provider_id: self.providers[provider_id].to_data(
                models=by_provider[provider_id]
            )
            for provider_id in sorted(by_provider)
        }


def _mutable_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_mutable_json(item) for item in value]
    return value
