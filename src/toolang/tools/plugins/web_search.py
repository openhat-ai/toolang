"""Default web search tool provider."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from toolang.concepts.tools import ToolDefinition
from toolang.errors import ToolangError

from ..contracts import ToolContext, ToolProvider

DEFAULT_TOP_K = 5


class WebSearchTool(ToolProvider):
    """Default DuckDuckGo-backed web search provider."""

    family = "web_search"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            family="web_search",
            name="web_search",
            description="Search the public web and return concise result snippets.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1},
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        query = _required_text(arguments, "query")
        top_k = _int_value(arguments.get("top_k"), default=DEFAULT_TOP_K)
        domains = _domains(arguments.get("domains"))
        raw_results = list(_search_text(query, max_results=max(top_k * 3, top_k)))
        filtered = []
        for item in raw_results:
            href = _normalized_text(item.get("href"))
            if href is None:
                continue
            if domains and not _matches_domains(href, domains):
                continue
            filtered.append(
                {
                    "title": _normalized_text(item.get("title")),
                    "url": href,
                    "snippet": _normalized_text(item.get("body")),
                }
            )
            if len(filtered) >= top_k:
                break
        return {
            "query": query,
            "domains": domains,
            "results": filtered,
        }


def create_web_search_tool(config: dict[str, Any]) -> ToolProvider:
    """Create the default web search tool provider."""

    return WebSearchTool()


def _search_text(query: str, *, max_results: int) -> list[dict[str, Any]]:
    try:
        from duckduckgo_search import DDGS
    except ImportError as exc:  # pragma: no cover - dependency environment
        raise ToolangError(
            "The 'duckduckgo-search' package is not installed. Run 'uv add duckduckgo-search' to enable web_search."
        ) from exc
    with DDGS() as searcher:
        results = searcher.text(query, max_results=max_results)
        return list(results)


def _required_text(arguments: dict[str, Any], name: str) -> str:
    value = str(arguments.get(name, "")).strip()
    if not value:
        raise ToolangError(f"web_search tool requires {name!r}")
    return value


def _int_value(value: object, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = value if isinstance(value, int) and not isinstance(value, bool) else int(str(value))
    except (TypeError, ValueError) as exc:
        raise ToolangError("web_search integer argument is invalid") from exc
    if parsed <= 0:
        raise ToolangError("web_search integer argument must be positive")
    return parsed


def _domains(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ToolangError("web_search domains must be a list of hostnames")
    result = []
    for item in value:
        domain = _normalized_text(item)
        if domain is None:
            continue
        result.append(domain.lower())
    return result


def _matches_domains(url: str, domains: list[str]) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains)


def _normalized_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
