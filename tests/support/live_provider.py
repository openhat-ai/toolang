"""Fixtures for opt-in tests backed by a real model provider."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from toolang.common.layout import AgentLayout
from toolang.lang import Program
from toolang.setup import AgentSetup, SetupWatcher
from toolang.state.state import AgentState, agent_state_revision

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
    root_revision = sha256(b"live-provider-smoke-root").hexdigest()
    home_revision = sha256(LIVE_PROVIDER_SOURCE.encode("utf-8")).hexdigest()
    state = AgentState(
        revision=agent_state_revision(root_revision, home_revision),
        root_revision=root_revision,
        home_revision=home_revision,
        root_config={},
        home_config={},
        config={},
        caps={},
        modules={"agent": program},
        module_sources={"agent": "agent.too"},
        module_digests={"agent": home_revision},
        module_caps={"agent": ()},
    )
    setup = await SetupWatcher(
        AgentLayout.resident(root, "alice"),
        binding_overrides={"model": model},
    ).refresh()
    return setup, state
