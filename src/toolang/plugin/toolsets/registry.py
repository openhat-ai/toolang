"""Tool registry names and selection helpers."""

from __future__ import annotations

from dataclasses import dataclass

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import AgentTool
from toolang.base.utils.tools import (
    encode_tool_name,
    is_internal_toolset_name,
    require_public_tool_name,
    require_toolset_name,
)
from toolang.plugin.loading import PluginSource


@dataclass(frozen=True, slots=True)
class ToolRef:
    """One structured public identity for a model-facing tool."""

    plugin: str
    toolset: str
    name: str

    @property
    def identity(self) -> str:
        return f"{self.toolset}/{self.name}"

    @property
    def model_name(self) -> str:
        return encode_tool_name(self.toolset, self.name)


def parse_tool_registration_key(
    plugin_name: str,
    key: str,
    leaf_tool_name: str,
    *,
    source: PluginSource,
) -> ToolRef:
    """Resolve one plugin tool key into a public tool ref."""

    require_toolset_plugin_name(plugin_name, source=source)
    if not isinstance(key, str):
        raise ToolangError(
            f"toolset plugin {plugin_name!r} returned a non-text tool key"
        )
    text = key
    if not text:
        raise ToolangError(f"toolset plugin {plugin_name!r} returned an empty tool key")
    if text.count("/") > 1:
        raise ToolangError(
            f"toolset plugin {plugin_name!r} returned an invalid tool key: {text!r}"
        )
    toolset, separator, name = text.partition("/")
    if not separator:
        toolset = plugin_name
        name = text
    if name != leaf_tool_name:
        raise ToolangError(
            f"toolset plugin {plugin_name!r} returned mismatched leaf tool name: "
            f"{text!r} != {leaf_tool_name!r}"
        )
    require_toolset_name(toolset)
    if source == "external" and is_internal_toolset_name(toolset):
        raise ToolangError(
            f"external toolset plugin {plugin_name!r} cannot register internal "
            f"toolset {toolset!r}"
        )
    require_public_tool_name(name, kind="tool")
    return ToolRef(plugin=plugin_name, toolset=toolset, name=name)


def require_toolset_plugin_name(plugin_name: str, *, source: PluginSource) -> None:
    """Require one effective toolset plugin identity allowed by its source."""

    if source == "built-in":
        require_toolset_name(plugin_name)
        return
    require_public_tool_name(plugin_name, kind="toolset plugin")


def tool_ref_for_model_tool(model_name: str, tool: AgentTool) -> ToolRef:
    """Return the structured public ref for one loaded model-facing tool."""

    ref = getattr(tool, "ref", None)
    if isinstance(ref, ToolRef):
        return ref
    plugin = getattr(tool, "plugin_name", None)
    plugin_name = plugin if isinstance(plugin, str) and plugin else "-"
    toolset = getattr(tool, "toolset", None)
    toolset_name = toolset if isinstance(toolset, str) and toolset else plugin_name
    leaf = _tool_leaf_name(tool)
    if toolset_name == "-" and "/" in model_name:
        toolset_name, _separator, leaf = model_name.partition("/")
    elif toolset_name == "-" and "__" in model_name:
        toolset_name, _separator, leaf = model_name.partition("__")
    elif toolset_name == "-" and "_" in model_name:
        toolset_name, _separator, leaf = model_name.partition("_")
    return ToolRef(plugin=plugin_name, toolset=toolset_name, name=leaf)


def _tool_leaf_name(tool: AgentTool) -> str:
    leaf_tool = getattr(tool, "leaf_tool", None)
    leaf = getattr(leaf_tool, "name", None)
    if isinstance(leaf, str) and leaf:
        return leaf
    name = getattr(tool, "name", "")
    return name if isinstance(name, str) and name else "-"
