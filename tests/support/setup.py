"""Helpers for creating an already-materialized setup in focused tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, cast

from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.model import Model, ModelOverride, Provider
from toolang.base.types.policy import RunDefaults, RunLimits
from toolang.common.layout import AgentLayout
from toolang.plugin.models.collections import ModelCollection
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.setup.types import AgentEnvironment, AgentSetup, _ModelData


def materialized_setup(
    *,
    layout: AgentLayout,
    providers: Mapping[str, Provider] | Sequence[Provider],
    adapters: Mapping[str, ModelAdapter],
    models: ModelCollection | Sequence[Model],
    tools: ToolCollection,
    envs: Mapping[str, str],
    revision: str = "test",
    environment: AgentEnvironment | None = None,
    defaults: RunDefaults | None = None,
    limits: RunLimits | None = None,
    compact_model: ModelOverride | None = None,
) -> AgentSetup:
    """Wrap fixture resources in the lazy AgentSetup public contract."""

    if isinstance(providers, Mapping):
        provider_records = tuple(cast(Mapping[str, Provider], providers).values())
    else:
        provider_records = tuple(cast(Sequence[Provider], providers))
    model_records = (
        models.entries if isinstance(models, ModelCollection) else tuple(models)
    )
    frozen_adapters = MappingProxyType(dict(adapters))
    frozen_tools = tools
    ready_models = tuple(
        model for model in model_records if model._toolang.effective_ready
    )
    ready_provider_ids = {model._toolang.provider for model in ready_models}
    data = _ModelData(
        models=model_records,
        providers=provider_records,
        models_effective=ready_models,
        providers_effective=tuple(
            provider
            for provider in provider_records
            if provider.id in ready_provider_ids
        ),
    )

    return AgentSetup(
        layout=layout,
        envs=envs,
        revision=revision,
        environment=environment,
        defaults=defaults if defaults is not None else RunDefaults(),
        limits=limits if limits is not None else RunLimits(),
        compact_model=compact_model,
        _load_models=lambda _setup: data,
        _load_tools=lambda _plugins: frozen_tools,
        _load_adapters=lambda: frozen_adapters,
        _load_catalogs=lambda: MappingProxyType({}),
        _load_toolset_plugins=lambda: MappingProxyType({}),
    )


_UNSET: Any = object()


def replace_materialized_setup(
    setup: AgentSetup,
    *,
    models=_UNSET,
    tools=_UNSET,
    adapters=_UNSET,
    defaults=_UNSET,
    compact_model=_UNSET,
) -> AgentSetup:
    """Replace selected materialized test views without restoring field access."""

    return materialized_setup(
        layout=setup.layout,
        providers=setup.providers_effective(),
        adapters=(
            setup.adapters()
            if adapters is _UNSET
            else cast(Mapping[str, ModelAdapter], adapters)
        ),
        models=(
            setup.models_effective()
            if models is _UNSET
            else cast(ModelCollection | Sequence[Model], models)
        ),
        tools=setup.tools() if tools is _UNSET else cast(ToolCollection, tools),
        envs=setup.envs,
        revision=setup.revision,
        environment=setup.environment,
        defaults=(
            setup.defaults if defaults is _UNSET else cast(RunDefaults, defaults)
        ),
        limits=setup.limits,
        compact_model=(
            setup.compact_model
            if compact_model is _UNSET
            else cast(ModelOverride | None, compact_model)
        ),
    )
