"""Tool provider loading helpers."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from toolang.concepts.identity import AgentRef
from toolang.concepts.tools import ToolDefinition, ToolFamily
from toolang.errors import ToolangError

from .contracts import ToolContext, ToolProvider, ToolProviderFactory
from .plugins.filesystem import create_filesystem_tool
from .plugins.service_use import create_service_use_tool
from .plugins.shell import create_shell_tool
from .plugins.web_search import create_web_search_tool

_DEFAULT_PROVIDER_BY_FAMILY: dict[ToolFamily, str] = {
    "filesystem": "default",
    "shell": "default",
    "service_use": "mcat",
    "web_search": "default",
}
_BUILTIN_TOOL_FACTORIES: dict[tuple[ToolFamily, str], ToolProviderFactory] = {
    ("filesystem", "default"): create_filesystem_tool,
    ("shell", "default"): create_shell_tool,
    ("service_use", "mcat"): create_service_use_tool,
    ("web_search", "default"): create_web_search_tool,
}


@dataclass(frozen=True, slots=True)
class ToolRuntime:
    """One resolved local tool set for a turn execution."""

    context: ToolContext
    providers: dict[ToolFamily, ToolProvider]

    def definitions(self) -> list[ToolDefinition]:
        """Return stable tool definitions for the current runtime."""

        return [self.providers[family].definition() for family in sorted(self.providers)]

    def enabled_families(self) -> list[ToolFamily]:
        """Return the enabled tool families in stable order."""

        return sorted(self.providers)


def load_tool_provider_factory(family: ToolFamily, provider: str) -> ToolProviderFactory:
    """Load one tool provider factory for a family/provider pair."""

    builtin = _BUILTIN_TOOL_FACTORIES.get((family, provider))
    if builtin is not None:
        return builtin
    entry_name = f"{family}:{provider}"
    for entry_point in entry_points(group="toolang.tool"):
        if entry_point.name == entry_name:
            loaded = entry_point.load()
            return loaded
    raise ToolangError(f"unknown tool provider: {family}:{provider}")


def create_tool_provider(
    family: ToolFamily,
    *,
    provider: str,
    config: dict[str, object] | None = None,
) -> ToolProvider:
    """Instantiate one named tool provider for a family."""

    factory = load_tool_provider_factory(family, provider)
    return factory(dict(config or {}))


def create_tool_runtime(
    agent: AgentRef,
    *,
    sandbox: str,
    working_directory: Path | None = None,
    visible_services: list[dict[str, Any]] | None = None,
) -> ToolRuntime:
    """Resolve one local tool runtime for the current agent turn."""

    context = ToolContext(
        agent=agent,
        working_directory=Path(working_directory or agent.home).expanduser().resolve(),
        sandbox=sandbox,
    )
    providers: dict[ToolFamily, ToolProvider] = {}
    for family, default_provider in _DEFAULT_PROVIDER_BY_FAMILY.items():
        provider_config: dict[str, object] = {}
        if family == "service_use":
            provider_config["visible_services"] = list(visible_services or [])
        providers[family] = create_tool_provider(
            family,
            provider=default_provider,
            config=provider_config,
        )
    return ToolRuntime(context=context, providers=providers)
