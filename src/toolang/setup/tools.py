"""One-shot effective tool loading without model setup construction."""

from __future__ import annotations

from toolang.common.layout import AgentLayout
from toolang.plugin.config import merge_plugin_configs
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.plugin.toolsets.loading import load_tools

from .config import load_agent_config, load_setup_config, resolve_setup_allow


def load_setup_tools(layout: AgentLayout) -> ToolCollection:
    """Load effective configured tools without constructing an AgentSetup."""

    configs = (load_setup_config(layout), load_agent_config(layout))
    tools = ToolCollection.from_tools(
        load_tools(
            toolset_config=merge_plugin_configs(configs, family="toolset"),
        )
    )
    allow = resolve_setup_allow(configs)
    if allow.tools is not None:
        tools = tools.match(allow.tools).compact()
    return tools
