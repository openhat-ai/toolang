"""One-shot effective tool loading tests."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from toolang.base.protocols.tool import AgentTool
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.common.layout import AgentLayout
from toolang.setup import tools as setup_tools


class _Tool(AgentTool):
    name = "shell__echo"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description="echo", parameters={})

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del arguments, context
        return {}


def test_tool_loader_applies_config_without_loading_models(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "toolang"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (root / "config.toml").write_text(
        '[allow]\ntools = ["shell/*"]\nmodels = ["provider/*"]\n',
        encoding="utf-8",
    )
    (home / "config.toml").write_text("", encoding="utf-8")
    layout = AgentLayout.resident(root, "alice")
    loaded: list[object] = []

    monkeypatch.setattr(
        setup_tools,
        "load_tools",
        lambda *, toolset_config: (
            loaded.append(toolset_config) or {"shell__echo": _Tool()}
        ),
    )

    tools = setup_tools.load_setup_tools(layout)

    assert tools.refs() == ("shell/echo",)
    assert loaded == [{}]
