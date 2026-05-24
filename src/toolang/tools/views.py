"""Display-oriented tool catalog views."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from toolang.base.protocols.tool import Tool
from toolang.tools.registry import (
    ToolRef,
    tool_ref_for_model_tool,
    tool_ref_matches,
)

MAX_DESCRIPTION_CHARS = 120


@dataclass(frozen=True, slots=True)
class _ToolCandidate:
    ref: ToolRef
    description: str


def tool_list_rows(
    *,
    tools: Mapping[str, Tool],
    plugin_sources: Mapping[str, str],
    selectors: Sequence[str] = (),
) -> list[tuple[str, str, str]]:
    """Return table rows for installed model-facing tool listings."""

    candidates = sorted(
        (
            _tool_candidate(name, tool, plugin_sources=plugin_sources)
            for name, tool in tools.items()
        ),
        key=lambda item: (item.ref.namespace, item.ref.name, item.ref.plugin),
    )
    selected = _select_candidates(candidates, selectors)
    return [
        (candidate.ref.namespace, candidate.ref.name, candidate.description)
        for candidate in selected
    ]


def _tool_candidate(
    name: str,
    tool: Tool,
    *,
    plugin_sources: Mapping[str, str],
) -> _ToolCandidate:
    ref = _tool_ref(name, tool, plugin_sources=plugin_sources)
    definition = tool.definition()
    return _ToolCandidate(
        ref=ref,
        description=_compact_description(definition.description),
    )


def _tool_ref(
    model_name: str,
    tool: Tool,
    *,
    plugin_sources: Mapping[str, str],
) -> ToolRef:
    ref = tool_ref_for_model_tool(model_name, tool)
    plugin_name = ref.plugin
    namespace_name = ref.namespace
    if plugin_name == "-" and namespace_name in plugin_sources:
        plugin_name = namespace_name
    return ToolRef(plugin=plugin_name, namespace=namespace_name, name=ref.name)


def _select_candidates(
    candidates: Sequence[_ToolCandidate],
    selectors: Sequence[str],
) -> tuple[_ToolCandidate, ...]:
    if not selectors:
        return tuple(candidates)
    selected: list[_ToolCandidate] = []
    seen: set[str] = set()
    for selector in selectors:
        text = selector.strip()
        if not text:
            continue
        for candidate in candidates:
            if candidate.ref.model_name in seen:
                continue
            if tool_ref_matches(candidate.ref, text):
                selected.append(candidate)
                seen.add(candidate.ref.model_name)
    return tuple(selected)


def _compact_description(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= MAX_DESCRIPTION_CHARS:
        return compact
    return f"{compact[: MAX_DESCRIPTION_CHARS - 1].rstrip()}..."
