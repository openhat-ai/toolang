"""Tool registry names and selection helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import AgentTool
from toolang.base.utils.tools import (
    encode_tool_name,
    is_internal_toolset_name,
    require_public_tool_name,
    require_toolset_name,
)
from toolang.common.selectors import (
    Selector,
    filter_value_matches,
    parse_selector,
    split_selector_list,
    selector_identity_matches,
)
from toolang.plugin.loading import PluginSource


@dataclass(frozen=True, slots=True)
class ToolRef:
    """One structured public identity for a model-facing tool."""

    plugin: str
    toolset: str
    name: str

    @property
    def selector(self) -> str:
        return f"{self.toolset}/{self.name}"

    @property
    def model_name(self) -> str:
        return encode_tool_name(self.toolset, self.name)


def split_tool_selectors(items: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Split repeated and CSV tool selector inputs."""

    return split_selector_list(items)


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


def tool_ref_matches(ref: ToolRef, selector: str) -> bool:
    """Return whether one public tool ref matches a selector."""

    if not selector.strip():
        return False
    parsed = parse_selector(selector, domain="tool")
    return tool_ref_matches_selector(ref, parsed)


def tool_ref_matches_selector(ref: ToolRef, selector: Selector) -> bool:
    """Return whether one public tool ref matches a parsed selector."""

    if not selector_identity_matches(
        family=ref.toolset,
        name=ref.name,
        selector=selector,
    ):
        return False
    for key, values in selector.filters.items():
        actual = _tool_filter_value(ref, key)
        if actual is None or not filter_value_matches(actual, values):
            return False
    return True


def selected_tool_names(
    refs_by_model_name: dict[str, ToolRef],
    selectors: Sequence[str],
) -> tuple[str, ...]:
    """Return model-facing tool names selected by public selectors."""

    selected: list[str] = []
    seen: set[str] = set()
    parsed_selectors = [
        parse_selector(selector, domain="tool")
        for selector in selectors
        if selector.strip()
    ]
    for selector in parsed_selectors:
        for model_name, ref in refs_by_model_name.items():
            if model_name in seen:
                continue
            if tool_ref_matches_selector(ref, selector):
                selected.append(model_name)
                seen.add(model_name)
    return tuple(selected)


def _tool_filter_value(ref: ToolRef, key: str) -> str | None:
    if key == "plugin":
        return ref.plugin
    return None


def _tool_leaf_name(tool: AgentTool) -> str:
    leaf_tool = getattr(tool, "leaf_tool", None)
    leaf = getattr(leaf_tool, "name", None)
    if isinstance(leaf, str) and leaf:
        return leaf
    name = getattr(tool, "name", "")
    return name if isinstance(name, str) and name else "-"
