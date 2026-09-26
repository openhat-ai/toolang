"""In-memory revision fingerprints for captured setup inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from toolang.base.types.model import Model, ModelCatalogSnapshot, ModelRoute, Provider
from toolang.common.cache import canonical_value, digest


def source_content_revision(snapshot: ModelCatalogSnapshot) -> str:
    """Return a deterministic content identity for one fresh catalog probe."""

    return digest(
        {
            "providers": [
                [provider_id, _provider_value(provider)]
                for provider_id, provider in snapshot.providers.items()
            ],
            "models": [_model_value(model) for model in snapshot.models],
            "local": snapshot.local,
        }
    )


def environment_identity(environ: Mapping[str, str]) -> dict[str, str]:
    """Digest captured environment values for an in-memory setup revision."""

    return {name: digest(value) for name, value in sorted(environ.items())}


def model_projection_key(
    *,
    kind: str,
    scope: str,
    catalog_revisions: Sequence[tuple[str, str]],
    setup_config: object,
    environment: Mapping[str, str],
    plugin_provenance: object,
    allow_models: Sequence[str] | None,
) -> str:
    """Return a content identity for one captured setup generation."""

    return digest(
        {
            "kind": kind,
            "scope": scope,
            "catalogs": [list(item) for item in catalog_revisions],
            "config": canonical_value(setup_config),
            "environment": environment,
            "provenance": canonical_value(plugin_provenance),
            "allow": None if allow_models is None else list(allow_models),
        }
    )


def _provider_value(provider: Provider) -> dict[str, object]:
    return {
        "public": provider.to_data(),
        "toolang": {
            "env": provider._toolang.env,
            "adapter": provider._toolang.adapter,
            "route": _route_value(provider._toolang.route),
        },
    }


def _model_value(model: Model) -> dict[str, object]:
    data = model.to_data()
    connection = model.provider
    if connection is not None and connection._toolang is not None:
        data["provider_connection_toolang"] = {
            "env": connection._toolang.env,
            "adapter": connection._toolang.adapter,
            "route": _route_value(connection._toolang.route),
        }
    data["toolang"] = {
        "provider": model._toolang.provider,
        "status": int(model._toolang.status),
        "route": _route_value(model._toolang.route),
    }
    return data


def _route_value(route: ModelRoute) -> dict[str, object]:
    return {
        "adapter": route.adapter,
        "api": route.api,
        "env": route.env,
        "headers": dict(route.headers),
        "options": dict(route.options),
    }
