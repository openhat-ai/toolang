"""Persistent complete model-list projections used by catalog inspection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import msgspec

from toolang.base.types.model import Model, ModelCatalogSnapshot, ModelRoute
from toolang.common.cache import digest, load_document, store_document
from toolang.plugin.models.collections import ModelQueryView, catalog_model_dataset
from toolang.setup.cache import _snapshot_document, _snapshot_from_data
from toolang.setup.cache_environment import environment_fingerprint

_LISTING_KIND = "model_listing"
_LISTING_KEY = "merged"
_LISTING_SCHEMA = 1
_PROJECTION_SCHEMA = 1


@dataclass(frozen=True, slots=True)
class ModelListStatus:
    """Secret-free readiness, policy, and route facts used by list output."""

    allowed: bool
    ready: bool
    adapter: str | None
    api_present: bool
    env_present: bool


@dataclass(frozen=True, slots=True)
class ModelCatalogListing:
    """Complete source catalog, query facts, and derived list status."""

    snapshot: ModelCatalogSnapshot
    query_views: tuple[ModelQueryView, ...]
    statuses: tuple[ModelListStatus, ...]
    allowed_refs: tuple[str, ...]
    default_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        refs = tuple(model.ref for model in self.snapshot.models)
        if tuple(view.key for view in self.query_views) != refs:
            raise ValueError("model-list query views do not match the full snapshot")
        if len(self.statuses) != len(refs):
            raise ValueError("model-list statuses do not match the full snapshot")
        known = set(refs)
        if not set(self.allowed_refs).issubset(known):
            raise ValueError("model-list allow refs are outside the full snapshot")
        if not set(self.default_refs).issubset(self.allowed_refs):
            raise ValueError("default model refs must be allowed")

    @property
    def status_by_ref(self) -> Mapping[str, ModelListStatus]:
        return {
            model.ref: status
            for model, status in zip(self.snapshot.models, self.statuses, strict=True)
        }

    def view(
        self, *, all_models: bool
    ) -> tuple[ModelCatalogSnapshot, tuple[ModelQueryView, ...]]:
        """Return the full catalog or its normal ready/allowed ordered subset."""

        if all_models:
            return self.snapshot, self.query_views
        by_model = {model.ref: model for model in self.snapshot.models}
        by_view = {view.key: view for view in self.query_views}
        models = tuple(by_model[ref] for ref in self.default_refs)
        provider_ids = {model._toolang.provider for model in models}
        providers = {
            provider_id: provider
            for provider_id, provider in self.snapshot.providers.items()
            if provider_id in provider_ids
        }
        snapshot = ModelCatalogSnapshot(
            providers=providers,
            models=models,
            revision=self.snapshot.revision,
            source=self.snapshot.source,
            local=self.snapshot.local,
        )
        return snapshot, tuple(by_view[ref] for ref in self.default_refs)


def listing_from_snapshot(
    snapshot: ModelCatalogSnapshot,
    *,
    allowed_refs: Sequence[str],
    default_refs: Sequence[str],
) -> ModelCatalogListing:
    """Project one complete routed snapshot into query and status facts."""

    dataset = catalog_model_dataset(snapshot)
    allowed = set(allowed_refs)
    views = cast(tuple[ModelQueryView, ...], dataset.items)
    statuses = tuple(
        ModelListStatus(
            allowed=model.ref in allowed,
            ready=model._toolang.ready,
            adapter=model._toolang.route.adapter,
            api_present=model._toolang.route.api is not None,
            env_present=model._toolang.route.env is not None,
        )
        for model in snapshot.models
    )
    return ModelCatalogListing(
        snapshot=snapshot,
        query_views=views,
        statuses=statuses,
        allowed_refs=tuple(allowed_refs),
        default_refs=tuple(default_refs),
    )


def _strip_routes(snapshot: ModelCatalogSnapshot) -> ModelCatalogSnapshot:
    """Drop runtime-only routes while keeping portable provider/model records."""

    providers = {
        key: replace(provider, _toolang=replace(provider._toolang, route=ModelRoute()))
        for key, provider in snapshot.providers.items()
    }
    models = tuple(model.with_route(ModelRoute()) for model in snapshot.models)
    return ModelCatalogSnapshot(
        providers=providers,
        models=models,
        revision=snapshot.revision,
        source=snapshot.source,
        local=snapshot.local,
    )


class ModelCatalogListingCache:
    """One replaceable complete derived listing in a setup model-cache directory."""

    def __init__(self, directory: Path) -> None:
        self._path = directory / "merged.json"

    def environment_names_for(
        self,
        *,
        static_revision: str,
        dynamic_revisions: Sequence[tuple[str, str]],
    ) -> tuple[str, ...] | None:
        """Reuse audited variable names only for matching source revisions."""

        try:
            document = load_document(self._path, kind=_LISTING_KIND, key=_LISTING_KEY)
            if document.get("listing_schema") != _LISTING_SCHEMA:
                return None
            base = document.get("base_inputs")
            names = document.get("environment_names")
            if not isinstance(base, dict) or not isinstance(names, list):
                return None
            base_values = cast(dict[str, object], base)
            sources = base_values.get("sources")
            expected = [("models_dev", static_revision), *dynamic_revisions]
            if sources != [list(item) for item in expected]:
                return None
            if any(not isinstance(name, str) for name in names):
                return None
            name_values = cast(list[str], names)
            return tuple(name_values)
        except Exception:
            return None

    def load(
        self,
        *,
        base_inputs: Mapping[str, object],
        environment_names: Sequence[str],
        environ: Mapping[str, str],
    ) -> ModelCatalogListing | None:
        """Load a validated listing only if current dependencies still match."""

        try:
            document = load_document(self._path, kind=_LISTING_KIND, key=_LISTING_KEY)
            if (
                document.get("listing_schema") != _LISTING_SCHEMA
                or document.get("projection_schema") != _PROJECTION_SCHEMA
                or document.get("base_inputs") != base_inputs
            ):
                return None
            stored_names = document.get("environment_names")
            if not isinstance(stored_names, list) or any(
                not isinstance(name, str) for name in stored_names
            ):
                return None
            if tuple(stored_names) != tuple(sorted(set(environment_names))):
                return None
            environment = environment_fingerprint(environment_names, environ)
            if document.get("environment") != [list(item) for item in environment]:
                return None
            if document.get("revision") != listing_revision(base_inputs, environment):
                return None
            return _listing_from_document(document)
        except Exception:
            # Cache data is untrusted; every validation or decode failure is a miss.
            return None

    def store(
        self,
        listing: ModelCatalogListing,
        *,
        base_inputs: Mapping[str, object],
        environment_names: Sequence[str],
        environ: Mapping[str, str],
    ) -> bool:
        """Store the full projection atomically without resolved route values."""

        names = tuple(sorted(set(environment_names)))
        environment = environment_fingerprint(names, environ)
        document = {
            "listing_schema": _LISTING_SCHEMA,
            "projection_schema": _PROJECTION_SCHEMA,
            "base_inputs": dict(base_inputs),
            "environment_names": list(names),
            "environment": [list(item) for item in environment],
            "revision": listing_revision(base_inputs, environment),
            "listing": _listing_to_data(listing),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return store_document(
            self._path,
            kind=_LISTING_KIND,
            key=_LISTING_KEY,
            document=document,
        )


def listing_revision(
    base_inputs: Mapping[str, object],
    environment: Sequence[tuple[str, str]],
) -> str:
    """Return the portable revision for the complete derived model listing."""

    return digest(
        {
            "projection_schema": _PROJECTION_SCHEMA,
            "base_inputs": base_inputs,
            "environment": [list(item) for item in environment],
        }
    )


def _listing_to_data(listing: ModelCatalogListing) -> dict[str, object]:
    return {
        # This codec omits resolved routes while preserving declared model data.
        "snapshot": _snapshot_document(listing.snapshot),
        "runtime": {
            model.ref: {
                "allowed": status.allowed,
                "ready": status.ready,
                "adapter": status.adapter,
                "api_present": status.api_present,
                "env_present": status.env_present,
            }
            for model, status in zip(
                listing.snapshot.models, listing.statuses, strict=True
            )
        },
        "query_views": [_query_view_to_data(view) for view in listing.query_views],
        "allowed_refs": list(listing.allowed_refs),
        "default_refs": list(listing.default_refs),
    }


def _listing_from_document(document: Mapping[str, object]) -> ModelCatalogListing:
    listing = document.get("listing")
    if not isinstance(listing, dict):
        raise TypeError("model-list cache listing must be an object")
    listing_values = cast(dict[str, object], listing)
    raw_snapshot = listing_values.get("snapshot")
    runtime = listing_values.get("runtime")
    raw_views = listing_values.get("query_views")
    allowed_refs = listing_values.get("allowed_refs")
    default_refs = listing_values.get("default_refs")
    if not isinstance(raw_snapshot, Mapping) or not isinstance(runtime, Mapping):
        raise TypeError("model-list cache snapshots/runtime must be objects")
    if (
        not isinstance(raw_views, list)
        or not isinstance(allowed_refs, list)
        or not isinstance(default_refs, list)
    ):
        raise TypeError("model-list cache indexes must be arrays")
    if any(not isinstance(value, str) for value in (*allowed_refs, *default_refs)):
        raise TypeError("model-list cache refs must be strings")
    revision = document.get("revision")
    if not isinstance(revision, str):
        raise TypeError("model-list cache revision must be text")
    snapshot = _snapshot_from_data(
        cast(Mapping[str, object], raw_snapshot), revision=revision
    )
    runtime_values = cast(Mapping[str, object], runtime)
    statuses: list[ModelListStatus] = []
    models: list[Model] = []
    for model in snapshot.models:
        facts = runtime_values.get(model.ref)
        if not isinstance(facts, dict):
            raise ValueError(f"missing runtime facts for {model.ref}")
        fact_values = cast(dict[str, object], facts)
        allowed = fact_values.get("allowed")
        ready = fact_values.get("ready")
        adapter = fact_values.get("adapter")
        api_present = fact_values.get("api_present")
        env_present = fact_values.get("env_present")
        if (
            not isinstance(allowed, bool)
            or not isinstance(ready, bool)
            or (adapter is not None and not isinstance(adapter, str))
            or not isinstance(api_present, bool)
            or not isinstance(env_present, bool)
        ):
            raise TypeError(f"invalid runtime facts for {model.ref}")
        route = ModelRoute(
            adapter=adapter,
            api="cached-present" if api_present else None,
            env=() if env_present else None,
        )
        if route.ready != ready:
            raise ValueError(f"inconsistent cached readiness for {model.ref}")
        models.append(model.with_route(route))
        statuses.append(
            ModelListStatus(allowed, ready, adapter, api_present, env_present)
        )
    snapshot = ModelCatalogSnapshot(
        providers=snapshot.providers,
        models=tuple(models),
        revision=snapshot.revision,
        source=snapshot.source,
        local=snapshot.local,
    )
    if any(not isinstance(value, Mapping) for value in raw_views):
        raise TypeError("model-list query view must be an object")
    by_ref = {model.ref: model for model in snapshot.models}
    views = tuple(
        _query_view_from_data(cast(Mapping[str, object], raw), by_ref)
        for raw in raw_views
    )
    return ModelCatalogListing(
        snapshot=snapshot,
        query_views=views,
        statuses=tuple(statuses),
        allowed_refs=tuple(cast(list[str], allowed_refs)),
        default_refs=tuple(cast(list[str], default_refs)),
    )


def _query_view_to_data(view: ModelQueryView) -> dict[str, object]:
    return {
        "key": view.key,
        "provider": view.provider,
        "model": view.model,
        "name": view.name,
        "description": view.description,
        "family": view.family,
        "available": view.available,
        "adapter": view.adapter,
        "catalog": view.catalog,
        "route": {"provider": view.route.provider, "adapter": view.route.adapter},
        "tags": list(view.tags),
        "streaming": view.streaming,
        "attachment": view.attachment,
        "reasoning": view.reasoning,
        "tool_call": view.tool_call,
        "structured_output": view.structured_output,
        "temperature": view.temperature,
        "open_weights": view.open_weights,
        "status": view.status,
        "release_date": view.release_date.isoformat() if view.release_date else None,
        "last_updated": view.last_updated.isoformat() if view.last_updated else None,
        "modalities": {
            "input": list(view.modalities.input),
            "output": list(view.modalities.output),
        },
        "limit": {"context": view.limit.context, "output": view.limit.output},
        "cost": {"input": view.cost.input, "output": view.cost.output},
        "parameters": {"reasoning": {"effort": list(view.parameters.reasoning.effort)}},
    }


def _query_view_from_data(
    raw: Mapping[str, object], models: Mapping[str, Model]
) -> ModelQueryView:
    key = raw.get("key")
    if not isinstance(key, str) or key not in models:
        raise ValueError("model-list query view has unknown key")
    decoded = msgspec.convert({**raw, "record": models[key]}, type=ModelQueryView)
    return decoded


__all__ = [
    "ModelCatalogListing",
    "ModelCatalogListingCache",
    "ModelListStatus",
    "listing_revision",
    "listing_from_snapshot",
]
