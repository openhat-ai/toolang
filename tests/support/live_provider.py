"""Fixtures for opt-in tests backed by a real model provider."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path

from toolang.common.layout import AgentLayout
from toolang.lang import Program
from toolang.plugin.models.loading import load_model_adapters, load_model_providers
from toolang.plugin.models.resolution import parse_model_selector
from toolang.setup import AgentSetup
from toolang.state.state import AgentState, agent_state_version

LIVE_PROVIDER_SOURCE = """
agic smoke(_: Text) -> Text:
  recall = none
  context: none
  instruct: Return the requested text exactly, without explanation.
  user: Return exactly this text: TOOLANG_RESPONSE {{_}}

flow relay(_: Text) -> Text:
  run smoke
"""

LIVE_RESPONSE_PREFIX = "TOOLANG_RESPONSE"


def create_live_agent(
    root: Path,
    *,
    model: str,
) -> tuple[AgentSetup, AgentState]:
    """Create fixed agent snapshots that resolve one real model selector."""

    program = Program.from_source(LIVE_PROVIDER_SOURCE)
    root_version = sha256(b"live-provider-smoke-root").digest()
    home_version = sha256(LIVE_PROVIDER_SOURCE.encode("utf-8")).digest()
    state = AgentState(
        version=agent_state_version(root_version, home_version),
        root_version=root_version,
        home_version=home_version,
        toolang_version="test",
        root_config={},
        home_config={},
        config={},
        program_source="agents/alice/agent.too",
        program=program,
        caps=(),
        loaded_at="2026-01-01T00:00:00Z",
    )
    providers = load_model_providers()
    selector = parse_model_selector(model)
    provider_filters = selector.filters.get("provider", ())
    provider_hint = (
        provider_filters[0]
        if len(provider_filters) == 1 and provider_filters[0] in providers
        else selector.pattern.partition("/")[0]
        if "/" in selector.pattern
        and selector.pattern.partition("/")[0] in providers
        else None
    )
    if provider_hint is not None:
        providers = {provider_hint: providers[provider_hint]}
    envs = dict(os.environ)
    setup = AgentSetup(
        layout=AgentLayout.resident(root, "alice"),
        providers=providers,
        adapters=load_model_adapters(),
        models=tuple(
            model_info
            for provider in providers.values()
            for model_info in provider.list_models(environ=envs)
        ),
        tools={},
        envs=envs,
    )
    return setup, state
