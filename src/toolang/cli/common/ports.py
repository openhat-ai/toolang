"""Resolve resident API port inputs at the CLI startup boundary."""

from collections.abc import Mapping

from toolang.common.config_sources import config_sources
from toolang.common.layout import AgentLayout
from toolang.up.config import agent_api_port, resolve_port_override


def agent_port(
    layout: AgentLayout, option: int | None, *, environ: Mapping[str, str]
) -> int | None:
    if layout.placement != "resident":
        return option
    configured = agent_api_port(config_sources(layout), home=layout.config)
    return resolve_port_override(
        option, environ=environ, env_name="TOOLANG_AGENT_PORT", configured=configured
    )
