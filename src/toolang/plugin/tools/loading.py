"""Tool plugin loading and runtime selection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import shlex
from typing import Any, cast

from toolang.base.protocols.tool import AgentTool, AgentToolSet
from toolang.base.types.tool import ToolContext, ToolDefinition

from toolang.plugin.config import load_tool_plugin_config
from toolang.plugin.loading import load_plugins
from toolang.state.agent import AgentState
from toolang.state.prepared import PreparedEntry
from .registry import (
    ToolRef,
    parse_tool_registration_key,
    selected_tool_names,
    tool_ref_for_model_tool,
)


@dataclass(frozen=True, slots=True)
class LoadedTool(AgentTool):
    """One model-facing tool loaded from a named plugin."""

    plugin_name: str
    ref: ToolRef
    leaf_tool: AgentTool

    @property
    def name(self) -> str:
        return self.ref.model_name

    @property
    def namespace(self) -> str:
        return self.ref.namespace

    @property
    def public_name(self) -> str:
        return self.ref.selector

    def definition(self) -> ToolDefinition:
        definition = self.leaf_tool.definition()
        return ToolDefinition(
            name=self.name,
            description=definition.description,
            parameters=dict(definition.parameters),
        )

    def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        return self.leaf_tool.invoke(arguments, context)


def load_tool_plugins(
    *,
    config: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, AgentTool]:
    tools: dict[str, AgentTool] = {}
    plugins = cast(
        dict[str, AgentToolSet],
        load_plugins(group="toolang.tool", config=config),
    )
    for plugin in plugins.values():
        for leaf_name, leaf_tool in plugin.tools().items():
            ref = parse_tool_registration_key(plugin.name, leaf_name, leaf_tool.name)
            loaded = LoadedTool(
                plugin_name=plugin.name,
                ref=ref,
                leaf_tool=leaf_tool,
            )
            if loaded.name in tools:
                raise ValueError(f"duplicate tool name: {loaded.public_name}")
            tools[loaded.name] = loaded
    return tools


def load_runtime_tools(
    *,
    root: Path,
    name: str,
    state: AgentState,
    environ: Mapping[str, str],
    selectors: Sequence[str] | None = None,
) -> dict[str, AgentTool]:
    tools = load_tool_plugins(
        config=runtime_tool_config(
            root=root,
            name=name,
            state=state,
            environ=environ,
        )
    )
    return select_tools(tools, selectors)


def runtime_tool_config(
    *,
    root: Path,
    name: str,
    state: AgentState,
    environ: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    config = load_tool_plugin_config(root, name, environ=environ)
    visible_services = [
        service
        for entry in state.caps
        if entry.kind == "service"
        if (service := _service_config(entry)) is not None
    ]
    if visible_services:
        service_use = dict(config.get("service_use", {}))
        service_use["visible_services"] = visible_services
        config["service_use"] = service_use
    return config


def select_tools(
    tools: dict[str, AgentTool],
    selectors: Sequence[str] | None,
) -> dict[str, AgentTool]:
    if selectors is None:
        return tools
    if not selectors:
        return {}
    refs = {name: tool_ref_for_model_tool(name, tool) for name, tool in tools.items()}
    return {
        name: tools[name]
        for name in selected_tool_names(refs, selectors)
        if name in tools
    }


def validate_tool_selectors(
    tools: dict[str, AgentTool],
    selectors: Sequence[str] | None,
) -> None:
    if not selectors:
        return
    refs = {name: tool_ref_for_model_tool(name, tool) for name, tool in tools.items()}
    missing = [
        selector
        for selector in selectors
        if not selected_tool_names(refs, (selector,))
    ]
    if missing:
        raise ValueError(f"tool selector matched no tools: {', '.join(missing)}")


def _service_config(entry: PreparedEntry) -> dict[str, object] | None:
    transport = _text(entry.meta.get("transport"))
    target = _text(entry.meta.get("target"))
    if transport not in {"http", "stdio"} or target is None:
        return None
    service: dict[str, object] = {
        "name": entry.name,
        "description": _text(entry.meta.get("description")),
        "transport": transport,
        "target": target,
    }
    if transport == "stdio":
        try:
            command = shlex.split(target)
        except ValueError:
            command = []
        if command:
            service["command"] = command
    env_vars = _env_names(entry.meta.get("env"))
    if env_vars:
        service["env_vars"] = env_vars
    return {key: value for key, value in service.items() if value is not None}


def _env_names(value: object) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None
