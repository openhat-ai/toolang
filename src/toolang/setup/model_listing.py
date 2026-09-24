"""Persistent complete model-list projections used by catalog inspection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import msgspec

from toolang.base.types.model import Model, ModelCatalogSnapshot, ModelRoute
from toolang.common.cache import (
    _contains_unsafe_headers,
    digest,
    load_document,
    store_document,
)
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


class ModelCatalogListingCache:
    """One replaceable complete derived listing in a setup model-cache directory."""

    def __init__(self, directory: Path) -> None:
        self._path = directory / "merged.json"

    def load_if_valid(
        self,
        *,
        base_inputs: Mapping[str, object],
        source_revisions: Sequence[tuple[str, str]],
        environ: Mapping[str, str],
    ) -> tuple[ModelCatalogListing | None, tuple[str, ...] | None]:
        """Read/validate one document and return a hit plus reusable env names."""

        try:
            document = load_document(self._path, kind=_LISTING_KIND, key=_LISTING_KEY)
            if (
                document.get("listing_schema") != _LISTING_SCHEMA
                or document.get("projection_schema") != _PROJECTION_SCHEMA
            ):
                return None, None
            stored_base = document.get("base_inputs")
            names = document.get("environment_names")
            if not isinstance(stored_base, dict) or not isinstance(names, list):
                return None, None
            stored_values = cast(dict[str, object], stored_base)
            expected_sources = [list(item) for item in source_revisions]
            if stored_values.get("sources") != expected_sources:
                return None, None
            if any(not isinstance(name, str) for name in names):
                return None, None
            environment_names = tuple(cast(list[str], names))
            if stored_values != base_inputs:
                return None, environment_names
            environment = environment_fingerprint(environment_names, environ)
            if document.get("environment") != [list(item) for item in environment]:
                return None, environment_names
            if document.get("revision") != listing_revision(base_inputs, environment):
                return None, environment_names
            # Validate envelope and freshness before decoding the multi-megabyte
            # model projection into Python records.
            payload = document.get("listing")
            if not isinstance(payload, Mapping):
                return None, environment_names
            if not _listing_payload_is_valid(cast(Mapping[str, object], payload)):
                return None, environment_names
            return _listing_from_document(document), environment_names
        except Exception:
            # Cache data is untrusted; every validation or decode failure is a miss.
            return None, None

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
    snapshot_data = _snapshot_document(listing.snapshot)
    raw_models = cast(list[dict[str, object]], snapshot_data["models"])
    # User-supplied provider overrides can carry credential-bearing headers or
    # bodies. The list projection needs the ordinary model facts only.
    for raw_model in raw_models:
        for field in ("headers", "body"):
            raw_model.pop(field, None)
        provider = raw_model.get("provider")
        if isinstance(provider, dict):
            provider_values = cast(dict[str, object], provider)
            provider_values.pop("headers", None)
            provider_values.pop("body", None)
    return {
        "snapshot": snapshot_data,
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


def _listing_payload_is_valid(payload: Mapping[str, object]) -> bool:
    """Check the cache's route-free nested tree before expensive hydration."""

    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return False
    snapshot_values = cast(Mapping[str, object], snapshot)
    providers = snapshot_values.get("providers")
    models = snapshot_values.get("models")
    runtime = payload.get("runtime")
    query_views = payload.get("query_views")
    allowed_refs = payload.get("allowed_refs")
    default_refs = payload.get("default_refs")
    if not isinstance(providers, Mapping) or not isinstance(models, list):
        return False
    if not isinstance(runtime, Mapping) or not isinstance(query_views, list):
        return False
    if not isinstance(allowed_refs, list) or not isinstance(default_refs, list):
        return False
    if _contains_unsafe_headers(snapshot):
        return False
    runtime_values = cast(Mapping[str, object], runtime)
    known_refs: set[str] = set()
    for model in models:
        if not isinstance(model, Mapping):
            return False
        model_values = cast(Mapping[str, object], model)
        toolang = model_values.get("_toolang")
        if not isinstance(toolang, Mapping):
            return False
        toolang_values = cast(Mapping[str, object], toolang)
        provider = toolang_values.get("provider")
        model_id = model_values.get("id")
        if not isinstance(provider, str) or not isinstance(model_id, str):
            return False
        ref = f"{provider}/{model_id}"
        if ref in known_refs:
            return False
        known_refs.add(ref)
        facts = runtime_values.get(ref)
        if not isinstance(facts, Mapping):
            return False
        fact_values = cast(Mapping[str, object], facts)
        if any(
            not isinstance(fact_values.get(field), bool)
            for field in ("allowed", "ready", "api_present", "env_present")
        ):
            return False
        adapter = fact_values.get("adapter")
        if adapter is not None and not isinstance(adapter, str):
            return False
    if set(runtime_values) != known_refs:
        return False
    if any(
        not isinstance(value, str) or value not in known_refs
        for value in (*allowed_refs, *default_refs)
    ):
        return False
    if any(not isinstance(view, Mapping) for view in query_views):
        return False
    return True


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
    raw_snapshot_values = cast(dict[str, object], raw_snapshot)
    for model in cast(list[object], raw_snapshot_values.get("models", [])):
        if not isinstance(model, dict):
            raise TypeError("model-list cache model must be an object")
        model_values = cast(dict[str, object], model)
        provider = model_values.get("provider")
        if isinstance(provider, Mapping) and (
            "headers" in provider or "body" in provider
        ):
            raise ValueError(
                "model-list cache must not contain provider headers or bodies"
            )
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
