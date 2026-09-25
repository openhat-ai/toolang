"""Flat provider/model records for the persistent runtime setup catalog."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, TypedDict, cast

import msgspec

from toolang.common.cache import load_document, require_fields, store_document
from toolang.plugin.models.collections import parse_model_query_date

from .cache_environment import environment_fingerprint

Text = Annotated[str, msgspec.Meta(min_length=1)]
Positive = Annotated[int, msgspec.Meta(gt=0)]
Rank = Annotated[int, msgspec.Meta(ge=0)]
_LISTING_SCHEMA = 2


class _ConnectionMetadata(TypedDict, total=False):
    adapter: str | None
    env: tuple[str | tuple[str, ...], ...]


class _Connection(TypedDict, total=False):
    """Validate nested declarations before accepting a cached model record."""

    npm: str | None
    api: str | None
    shape: str | None
    mode: str | None
    headers: Mapping[str, str] | None
    body: Mapping[str, object] | None
    _toolang: _ConnectionMetadata | None


class ProviderRecord(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, omit_defaults=True
):
    """One provider declaration without embedded models or resolved routes."""

    id: Text
    name: Text
    env: tuple[str, ...] = ()
    npm: str | None = None
    api: str | None = None
    doc: str | None = None
    adapter: str | None = None
    env_rule: tuple[str | tuple[str, ...], ...] = ()


class ModelRecord(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, omit_defaults=True
):
    """One model's declarations and compact inspection state."""

    id: Text
    provider: Text
    name: Text
    adapter: str | None
    api_present: bool
    env_present: bool
    allowed_order: Rank | None
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
    modalities: Mapping[str, tuple[str, ...]] = msgspec.field(default_factory=dict)
    open_weights: bool | None = None
    limit: Mapping[str, Positive] = msgspec.field(default_factory=dict)
    status: str | None = None
    experimental: Mapping[str, object] | None = None
    connection: _Connection | None = None
    cost: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        parse_model_query_date(self.release_date)
        parse_model_query_date(self.last_updated)
        msgspec.structs.force_setattr(
            self,
            "modalities",
            MappingProxyType(
                {name: tuple(values) for name, values in self.modalities.items()}
            ),
        )
        msgspec.structs.force_setattr(self, "limit", MappingProxyType(dict(self.limit)))
        for name in (
            "reasoning_options",
            "interleaved",
            "experimental",
            "connection",
            "cost",
        ):
            value = getattr(self, name)
            if value is not None:
                msgspec.structs.force_setattr(self, name, _freeze(value))

    @property
    def model(self) -> str:
        """Expose the model component of the existing query identity."""

        return self.id

    @property
    def ready(self) -> bool:
        return self.adapter is not None and self.api_present and self.env_present


def _freeze(value: object) -> object:
    """Detach nested declarations before publishing them in an immutable setup."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


class CatalogRecords(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Each provider and model occurs once; indexes remain consumer-owned."""

    providers: tuple[ProviderRecord, ...]
    models: tuple[ModelRecord, ...]

    def __post_init__(self) -> None:
        providers = {provider.id for provider in self.providers}
        if len(providers) != len(self.providers):
            raise ValueError("duplicate catalog provider")
        identities: set[tuple[str, str]] = set()
        ranks: set[int] = set()
        for model in self.models:
            identity = (model.provider, model.id)
            if model.provider not in providers or identity in identities:
                raise ValueError(
                    "invalid catalog model ownership or duplicate identity"
                )
            identities.add(identity)
            if model.allowed_order is not None:
                if model.allowed_order in ranks:
                    raise ValueError("duplicate catalog policy rank")
                ranks.add(model.allowed_order)
        if ranks != set(range(len(ranks))):
            raise ValueError("catalog policy ranks must be contiguous")


class _Metadata(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    version: int
    inputs: dict[str, object]
    environment_names: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]


class _Document(msgspec.Struct, forbid_unknown_fields=True):
    schema: int
    kind: str
    key: str
    metadata: _Metadata
    providers: msgspec.Raw
    models: msgspec.Raw


_DOCUMENT_DECODER = msgspec.json.Decoder(_Document)
_PROVIDER_DECODER = msgspec.json.Decoder(tuple[ProviderRecord, ...])
_MODEL_DECODER = msgspec.json.Decoder(tuple[ModelRecord, ...])


def _decode_document(payload: str) -> dict[str, object]:
    # Check dependencies before allocating model records. Native decoding avoids
    # an intermediate catalog-sized tree of dictionaries and a conversion pass.
    return msgspec.structs.asdict(_DOCUMENT_DECODER.decode(payload))


class ModelListingCache:
    """One atomically replaced listing per root or agent, including --catalog."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "merged.json"

    def load(
        self, *, inputs: Mapping[str, object], environ: Mapping[str, str]
    ) -> CatalogRecords | None:
        """Validate dependencies before decoding the flat typed model records."""

        try:
            document = load_document(
                self.path,
                kind="model_listing",
                key="merged",
                scan_content=False,
                decode=_decode_document,
            )
            require_fields(
                document,
                frozenset({"schema", "kind", "key", "metadata", "providers", "models"}),
                label="model listing",
            )
            metadata = msgspec.convert(document["metadata"], type=_Metadata)
            if (
                metadata.version != _LISTING_SCHEMA
                or metadata.inputs != inputs
                or metadata.environment
                != environment_fingerprint(metadata.environment_names, environ)
            ):
                return None
            return CatalogRecords(
                providers=_PROVIDER_DECODER.decode(
                    cast(msgspec.Raw, document["providers"])
                ),
                models=_MODEL_DECODER.decode(cast(msgspec.Raw, document["models"])),
            )
        except Exception:
            # Cache damage must never make an otherwise valid source unreadable.
            return None

    def store(
        self,
        records: CatalogRecords,
        *,
        inputs: Mapping[str, object],
        environment_names: Sequence[str],
        environ: Mapping[str, str],
    ) -> bool:
        """Persist declarations and environment hashes, never resolved env values."""

        names = tuple(sorted(set(environment_names)))
        metadata = _Metadata(
            version=_LISTING_SCHEMA,
            inputs=dict(inputs),
            environment_names=names,
            environment=environment_fingerprint(names, environ),
        )
        return store_document(
            self.path,
            kind="model_listing",
            key="merged",
            scan_content=False,
            document={
                "metadata": metadata,
                "providers": records.providers,
                "models": records.models,
            },
        )
