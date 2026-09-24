"""Auditable environment dependencies for persistent model-list caches."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
from string import Template

from toolang.base.types.model import ModelCatalogSnapshot


def environment_names(snapshot: ModelCatalogSnapshot) -> tuple[str, ...]:
    """Return sorted declared variables that can affect resolved model routes."""

    names: set[str] = set()
    for provider in snapshot.providers.values():
        names.update(provider.env)
        for alternative in provider._toolang.env:
            if isinstance(alternative, str):
                names.add(alternative)
            else:
                names.update(alternative)
        names.update(_template_names(provider.api))
    for model in snapshot.models:
        override = model.provider
        if override is not None:
            names.update(_template_names(override.api))
    return tuple(sorted(names))


def environment_fingerprint(
    names: Sequence[str],
    environ: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    """Hash current values for selected names; never return or retain plaintext."""

    return tuple(
        (name, sha256(environ[name].encode("utf-8")).hexdigest())
        for name in sorted(set(names))
        if name in environ
    )


def _template_names(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    names: set[str] = set()
    for match in Template.pattern.finditer(value):
        name = match.group("named") or match.group("braced")
        if name is not None:
            names.add(name)
    return tuple(sorted(names))


__all__ = ["environment_fingerprint", "environment_names"]
