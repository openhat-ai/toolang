"""Fixtures for opt-in tests backed by a real model provider."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from toolang.common.layout import AgentLayout
from toolang.lang import Program
from toolang.setup import AgentSetup, SetupWatcher
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


async def create_live_agent(
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
    setup = await SetupWatcher(
        AgentLayout.resident(root, "alice"),
        binding_overrides={"model": model},
    ).refresh()
    return setup, state
