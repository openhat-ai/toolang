"""Capture tool resources without loading unrelated model catalogs or adapters."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from types import MappingProxyType

from toolang.common.cache import digest
from toolang.common.config_sources import config_sources
from toolang.common.layout import AgentLayout
from toolang.common.progress import ProgressSink
from toolang.plugin.config import merge_plugin_configs
from toolang.plugin.loading import plugin_provenance
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.plugin.toolsets.loading import (
    load_toolsets_with_sources,
    tools_from_toolsets,
)
from toolang.plugin.types import LoadedPlugin

from .config import resolve_tool_allow
from .progress import setup_progress
from .teaming import resolve_teaming_setup
from .types import ToolSetup


async def load_tool_setup(
    layout: AgentLayout,
    *,
    agent_context: bool = True,
    progress: ProgressSink | None = None,
) -> ToolSetup:
    """Capture one tool view through the same ownership and policy as AgentSetup."""
    with setup_progress(progress, target=layout.name, resource="tool setup"):
        sources = config_sources(layout, include_agent=agent_context)
        configs = tuple(source.config for source in sources)
        toolsets = merge_plugin_configs(configs, family="toolset")
        teaming = resolve_teaming_setup(
            sources, root=layout.root_config, home=layout.config
        )
        toolsets["msg"] = teaming.toolset_config(root=layout.root)
        allowed = resolve_tool_allow(configs)
        revision = digest(
            {
                "scope": "agent" if agent_context else "root",
                "toolsets": toolsets,
                "allow": allowed,
                "plugins": [
                    item.to_data()
                    for item in plugin_provenance(group="toolang.toolset")
                ],
            }
        )
        return ToolSetup(
            layout=layout,
            revision=revision,
            _load_tools=materialize_tools,
            _load_toolset_plugins=capture_toolset_loader(toolsets),
            _allowed_tools=allowed,
        )


def capture_toolset_loader(
    configs: Mapping[str, Mapping[str, object]],
) -> Callable[[], Mapping[str, LoadedPlugin]]:
    """Keep a generation's plugin inputs isolated from later configuration edits."""
    captured = deepcopy({name: dict(value) for name, value in configs.items()})

    def load() -> Mapping[str, LoadedPlugin]:
        return MappingProxyType(load_toolsets_with_sources(config=captured))

    return load


def materialize_tools(plugins: Mapping[str, LoadedPlugin]) -> ToolCollection:
    """Build the shared pre-allow tool collection for one setup generation."""
    return ToolCollection.from_tools(tools_from_toolsets(plugins))
