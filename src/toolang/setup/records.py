"""Flat provider/model records for the persistent inspection catalog."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated

import msgspec

from toolang.common.cache import load_document, require_fields, store_document
from toolang.plugin.models.collections import parse_model_query_date

from .cache_environment import environment_fingerprint

Text = Annotated[str, msgspec.Meta(min_length=1)]
Positive = Annotated[int, msgspec.Meta(gt=0)]
Rank = Annotated[int, msgspec.Meta(ge=0)]
_LISTING_SCHEMA = 3


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
    reasoning_options: tuple[dict[str, object], ...] | None = None
    tool_call: bool | None = None
    interleaved: bool | dict[str, object] | None = None
    structured_output: bool | None = None
    temperature: bool | None = None
    knowledge: str | None = None
    release_date: str | None = None
    last_updated: str | None = None
    modalities: dict[str, tuple[str, ...]] = msgspec.field(default_factory=dict)
    open_weights: bool | None = None
    limit: dict[str, Positive] = msgspec.field(default_factory=dict)
    status: str | None = None
    experimental: dict[str, object] | None = None
    connection: dict[str, object] | None = None
    cost: dict[str, object] | None = None

    def __post_init__(self) -> None:
        parse_model_query_date(self.release_date)
        parse_model_query_date(self.last_updated)

    @property
    def model(self) -> str:
        """Expose the model component of the existing query identity."""

        return self.id

    @property
    def ready(self) -> bool:
        return self.adapter is not None and self.api_present and self.env_present


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
                self.path, kind="model_listing", key="merged", scan_content=False
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
            return msgspec.convert(
                {"providers": document["providers"], "models": document["models"]},
                type=CatalogRecords,
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
