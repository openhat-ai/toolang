"""Shared model catalog, alias, and execution value types."""

from __future__ import annotations

from collections.abc import Mapping
from copy import copy
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Self, TypeAlias

ResolvedEnv = tuple[str | tuple[str, ...], ...]
ReasoningEffort: TypeAlias = Literal[
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "default",
]
_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "default"}
)


@dataclass(frozen=True, slots=True)
class ReasoningParameters:
    """Reasoning controls requested for one model selection."""

    effort: ReasoningEffort | None = None

    def __post_init__(self) -> None:
        if self.effort is not None and self.effort not in _REASONING_EFFORTS:
            raise ValueError(f"unknown reasoning effort: {self.effort!r}")


@dataclass(frozen=True, slots=True)
class ModelParameters:
    """Typed call parameters attached to one model request."""

    reasoning: ReasoningParameters | None = None

    def __post_init__(self) -> None:
        if self.reasoning is not None and not isinstance(
            self.reasoning, ReasoningParameters
        ):
            raise TypeError("model reasoning parameters must be ReasoningParameters")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One exact model ref and its typed call parameters."""

    ref: str
    parameters: ModelParameters = ModelParameters()

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
        if not isinstance(self.parameters, ModelParameters):
            raise TypeError("model request parameters must be ModelParameters")


@dataclass(frozen=True, slots=True)
class Model:
    """One models.dev-compatible model record within a provider."""

    provider_id: str
    id: str
    name: str
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
    provider: Mapping[str, object] | None = None
    cost: Mapping[str, object] | None = None
    extra: Mapping[str, object] = field(default_factory=dict)
    local: bool = False
    catalog: str | None = None
    catalog_revision: str | None = None
    resolved: ResolvedModel | None = None

    def __post_init__(self) -> None:
        if not self.provider_id or not self.id or not self.name:
            raise ValueError("model provider_id, id, and name are required")
        object.__setattr__(
            self,
            "modalities",
            MappingProxyType(
                {str(key): tuple(value) for key, value in self.modalities.items()}
            ),
        )
        object.__setattr__(self, "limit", MappingProxyType(dict(self.limit)))
        object.__setattr__(self, "extra", _immutable_mapping(self.extra))
        if self.reasoning_options is not None:
            object.__setattr__(
                self,
                "reasoning_options",
                tuple(_immutable_mapping(option) for option in self.reasoning_options),
            )
        for name in ("experimental", "provider", "cost"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _immutable_mapping(value))
        if isinstance(self.interleaved, Mapping):
            object.__setattr__(
                self, "interleaved", _immutable_mapping(self.interleaved)
            )

    @property
    def identity(self) -> str:
        """Return the exact provider/model catalog identity."""

        return f"{self.provider_id}/{self.id}"

    def with_resolution(self, resolved: ResolvedModel) -> Self:
        """Attach runtime resolution without rebuilding frozen catalog fields."""

        result = copy(self)
        object.__setattr__(result, "resolved", resolved)
        return result

    def to_data(self) -> dict[str, object]:
        """Return this model in models.dev-compatible JSON form."""

        data = {key: _mutable_json(value) for key, value in self.extra.items()}
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
            "provider": self.provider,
            "cost": self.cost,
        }
        data.update({key: _mutable_json(value) for key, value in optional.items()})
        return {key: value for key, value in data.items() if value is not None}


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """One model's immutable load-time protocol route."""

    adapter: str | None
    api: str | None
    ready: bool


@dataclass(frozen=True, slots=True)
class ResolvedProvider:
    """One provider's immutable load-time runtime resolution."""

    adapter: str | None
    api: str | None
    env: ResolvedEnv
    ready: bool

    def __post_init__(self) -> None:
        normalized: list[str | tuple[str, ...]] = []
        for alternative in self.env:
            if isinstance(alternative, str):
                name = alternative.strip()
                if not name:
                    raise ValueError("resolved provider env names must be non-empty")
                normalized.append(name)
                continue
            group = tuple(name.strip() for name in alternative if name.strip())
            if not group:
                raise ValueError("resolved provider env groups must be non-empty")
            normalized.append(group[0] if len(group) == 1 else group)
        object.__setattr__(self, "env", tuple(normalized))


@dataclass(frozen=True, slots=True)
class Provider:
    """One models.dev-compatible provider and its model catalog entries."""

    id: str
    name: str
    env: tuple[str, ...]
    npm: str
    models: Mapping[str, Model]
    api: str | None = None
    doc: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)
    local: bool = False
    catalog: str | None = None
    catalog_revision: str | None = None
    resolved: ResolvedProvider | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.name or not self.npm:
            raise ValueError("provider id, name, and npm are required")
        normalized = dict(self.models)
        if any(key != model.id for key, model in normalized.items()):
            raise ValueError(f"provider {self.id!r} model keys must match model ids")
        if any(model.provider_id != self.id for model in normalized.values()):
            raise ValueError(f"provider {self.id!r} contains foreign models")
        object.__setattr__(self, "models", MappingProxyType(normalized))
        object.__setattr__(self, "extra", _immutable_mapping(self.extra))

    def to_data(
        self, *, models: Mapping[str, Model] | None = None
    ) -> dict[str, object]:
        """Return this provider in models.dev-compatible JSON form."""

        data = {key: _mutable_json(value) for key, value in self.extra.items()}
        data.update(
            {
                "id": self.id,
                "name": self.name,
                "env": list(self.env),
                "npm": self.npm,
                "models": {
                    key: model.to_data()
                    for key, model in sorted((models or self.models).items())
                },
            }
        )
        if self.api is not None:
            data["api"] = self.api
        if self.doc is not None:
            data["doc"] = self.doc
        return data


@dataclass(frozen=True, slots=True)
class ModelCatalogSnapshot:
    """One immutable model catalog and availability snapshot."""

    providers: Mapping[str, Provider]
    models: tuple[Model, ...]
    revision: str
    source: Path | None = None

    def __post_init__(self) -> None:
        providers = dict(self.providers)
        models = tuple(self.models)
        if any(key != provider.id for key, provider in providers.items()):
            raise ValueError("catalog provider keys must match provider ids")
        identities = [(model.provider_id, model.id) for model in models]
        if len(identities) != len(set(identities)):
            raise ValueError("catalog models must have unique provider/model identity")
        object.__setattr__(self, "providers", MappingProxyType(providers))
        object.__setattr__(self, "models", models)

    def find(self, provider_id: str, model_id: str) -> Model | None:
        """Find one exact model in this snapshot."""

        provider = self.providers.get(provider_id)
        return provider.models.get(model_id) if provider is not None else None

    def to_data(
        self,
        *,
        models: tuple[Model, ...] | None = None,
    ) -> dict[str, object]:
        """Return a complete models.dev-compatible provider map."""

        selected = self.models if models is None else models
        by_provider: dict[str, dict[str, Model]] = {}
        for model in selected:
            if model.local:
                raise ValueError(
                    f"local-only model cannot be exported: {model.identity}"
                )
            by_provider.setdefault(model.provider_id, {})[model.id] = model
        return {
            provider_id: self.providers[provider_id].to_data(
                models=by_provider[provider_id]
            )
            for provider_id in sorted(by_provider)
        }


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One provider-scoped model info entry."""

    ref: str
    provider: str
    name: str
    model: str
    selectors: tuple[str, ...] = field(default_factory=tuple)
    adapter: str = "default"
    scope: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    tools: bool = True
    streaming: bool = True
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_price: float | None = None
    output_price: float | None = None
    details: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_selector(self) -> str:
        """Return the preferred selector for display surfaces."""

        for selector in self.selectors:
            text = selector.strip()
            if text:
                return text
        return self.ref

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> Self:
        """Build one model info from persisted protocol-neutral data."""

        selectors = data.get("selectors", ())
        tags = data.get("tags", ())
        metadata = data.get("metadata", {})
        if not isinstance(selectors, list | tuple):
            raise TypeError("model selectors must be a list")
        if not isinstance(tags, list | tuple):
            raise TypeError("model tags must be a list")
        if not isinstance(metadata, Mapping):
            raise TypeError("model metadata must be an object")
        return cls(
            ref=str(data["ref"]),
            provider=str(data["provider"]),
            name=str(data["name"]),
            model=str(data["model"]),
            selectors=tuple(str(item) for item in selectors),
            adapter=str(data.get("adapter") or "default"),
            scope=str(data["scope"]) if data.get("scope") is not None else None,
            tags=tuple(str(item) for item in tags),
            tools=bool(data.get("tools", True)),
            streaming=bool(data.get("streaming", True)),
            context_window=_optional_int(data.get("context_window")),
            max_output_tokens=_optional_int(data.get("max_output_tokens")),
            input_price=_optional_float(data.get("input_price")),
            output_price=_optional_float(data.get("output_price")),
            details=str(data["details"]) if data.get("details") is not None else None,
            metadata={str(key): value for key, value in metadata.items()},
        )

    def to_data(self) -> dict[str, object]:
        """Return persisted protocol-neutral data for this model."""

        return {
            "ref": self.ref,
            "provider": self.provider,
            "name": self.name,
            "model": self.model,
            "selectors": list(self.selectors),
            "adapter": self.adapter,
            "scope": self.scope,
            "tags": list(self.tags),
            "tools": self.tools,
            "streaming": self.streaming,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "input_price": self.input_price,
            "output_price": self.output_price,
            "details": self.details,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ModelAlias:
    """One named local alias to a selectable model target."""

    name: str
    ref: str
    provider: str
    model: str | None = None
    display_name: str | None = None
    adapter: str | None = None
    endpoint: str | None = None
    key_env: str | None = None
    scope: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    tools: bool | None = None
    streaming: bool | None = None
    headers: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    details: str | None = None


@dataclass(frozen=True, slots=True)
class ModelTarget:
    """One fully resolved execution target for one runtime call."""

    ref: str
    provider: str
    name: str
    model: str
    adapter: str
    base_url: str | None = None
    api_key: str | None = None
    scope: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    headers: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    tools: bool = True
    streaming: bool = True
    structured_output: bool | None = None
    catalog: str | None = None
    catalog_revision: str | None = None
    reasoning: dict[str, Any] = field(default_factory=dict)
    mode: str | None = None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("model integer fields must be integers")
    return value


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("model price fields must be numbers")
    return float(value)


def _mutable_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_mutable_json(item) for item in value]
    if isinstance(value, Decimal):
        return value
    return value


def _immutable_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {str(key): _immutable_json(item) for key, item in value.items()}
    )


def _immutable_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _immutable_json(item) for key, item in value.items()}
        )
    if isinstance(value, tuple | list):
        return tuple(_immutable_json(item) for item in value)
    return value
