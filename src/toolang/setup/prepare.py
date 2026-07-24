"""Build immutable installed runtime setup snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from toolang.base.protocols.model import ModelAdapter, ModelProvider
from toolang.base.protocols.tool import AgentTool

from .models import ModelListCache, discover_models
from .types import AgentSetup


async def prepare_agent_setup(
    *,
    name: str,
    home: Path,
    providers: Mapping[str, ModelProvider],
    adapters: Mapping[str, ModelAdapter],
    tools: Mapping[str, AgentTool],
    envs: Mapping[str, str],
    cache: ModelListCache,
    refresh_models: bool = False,
) -> AgentSetup:
    """Discover available models and capture one immutable runtime setup."""

    models = await discover_models(
        providers,
        envs=envs,
        cache=cache,
        refresh=refresh_models,
    )
    return AgentSetup(
        name=name,
        home=home,
        providers=providers,
        adapters=adapters,
        models=tuple(
            model
            for model in models
            if model.adapter in adapters
        ),
        tools=tools,
        envs=envs,
    )
