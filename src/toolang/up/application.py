"""Assemble the same Agent API services for every execution host."""

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import cast

from fastapi import FastAPI
from toolang.api.app import DEFAULT_CORS_ORIGINS, create_app
from toolang.catalog import CapsManager, ChoresManager
from toolang.common.config_sources import config_sources
from toolang.stats.config import stats_config
from toolang.stats.service import StatsService
from .core import AgentCore


def create_agent_application(
    core: AgentCore,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    cors_allowed_origins: Sequence[str] = DEFAULT_CORS_ORIGINS,
) -> FastAPI:
    """Build the complete API; the execution host owns service start and close."""
    app = create_app(
        core,
        CapsManager(core.layout),
        ChoresManager(core.layout),
        lifespan=lifespan,
        cors_allowed_origins=cors_allowed_origins,
    )
    app.state.stats = StatsService(
        core.store.db_path,
        core.layout.stats_store,
        config=stats_config(config_sources(core.layout), home=core.layout.config),
    )
    return app


def application_statistics(app: FastAPI) -> StatsService:
    return cast(StatsService, app.state.stats)
