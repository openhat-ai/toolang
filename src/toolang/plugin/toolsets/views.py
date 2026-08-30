"""Display-oriented tool catalog views."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from toolang.base.protocols.tool import AgentTool
from toolang.plugin.toolsets.collections import tool_dataset


def tool_list_rows(
    *,
    tools: Mapping[str, AgentTool],
    plugin_sources: Mapping[str, str],
    queries: Sequence[str] = (),
) -> list[tuple[str, str, str, str, str]]:
    """Return table rows for installed model-facing tool listings."""

    dataset = tool_dataset(tools, plugin_sources=plugin_sources)
    selected = dataset.query(queries or None)
    _headers, rows = dataset.table(selected)
    return cast(list[tuple[str, str, str, str, str]], list(rows))
