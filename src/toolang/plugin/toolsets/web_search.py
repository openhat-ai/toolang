"""Web-search toolset plugin."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from anyio import to_process

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import AgentTool, Toolset
from toolang.base.utils.function_tools import create_function_tool, tool

DEFAULT_TOP_K = 5
DEFAULT_TIMEOUT = 15


@dataclass(slots=True)
class WebSearchToolset:
    """Public-web search tools."""

    config: dict[str, Any]
    name: str = "web"
    description: str | None = (
        "Search the public web and return concise result snippets."
    )
    _top_k: int = field(init=False, repr=False)
    _timeout: int = field(init=False, repr=False)
    _tools: dict[str, AgentTool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._top_k = _int_value(self.config.get("top_k"), default=DEFAULT_TOP_K)
        self._timeout = _int_value(
            self.config.get("timeout"),
            default=DEFAULT_TIMEOUT,
        )
        self._tools = self._build_tools()

    def tools(self) -> Mapping[str, AgentTool]:
        return dict(self._tools)

    def _build_tools(self) -> dict[str, AgentTool]:
        @tool(name="search", description="Search the public web.")
        async def search(
            query: str,
            top_k: int = self._top_k,
            domains: list[str] | None = None,
        ) -> dict[str, Any]:
            limit = _int_value(top_k, default=self._top_k)
            normalized_domains = _domains(domains)
            try:
                async with asyncio.timeout(self._timeout):
                    raw_results = await _run_search(
                        query,
                        max_results=max(limit * 3, limit),
                        timeout=min(self._timeout, 5),
                    )
            except TimeoutError as exc:
                raise ToolangError(
                    f"web search timed out after {self._timeout}s"
                ) from exc
            filtered: list[dict[str, str | None]] = []
            for item in raw_results:
                href = _normalized_text(item.get("href"))
                if href is None:
                    continue
                if normalized_domains and not _matches_domains(
                    href, normalized_domains
                ):
                    continue
                filtered.append(
                    {
                        "title": _normalized_text(item.get("title")),
                        "url": href,
                        "snippet": _normalized_text(item.get("body")),
                    }
                )
                if len(filtered) >= limit:
                    break
            return {
                "query": query,
                "domains": normalized_domains,
                "results": filtered,
            }

        return {"search": create_function_tool(search)}


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Create the web_search toolset plugin."""

    return WebSearchToolset(config=dict(config))


async def _run_search(
    query: str,
    *,
    max_results: int,
    timeout: int,
) -> list[dict[str, Any]]:
    return await to_process.run_sync(
        _search_text,
        query,
        max_results,
        timeout,
        cancellable=True,
    )


def _search_text(
    query: str,
    max_results: int,
    timeout: int,
) -> list[dict[str, Any]]:
    try:
        from ddgs import DDGS
    except ImportError as exc:  # pragma: no cover
        raise ToolangError(
            "The 'ddgs' package is not installed. Install Toolang dependencies to enable web_search."
        ) from exc
    with DDGS(timeout=timeout) as searcher:
        return list(searcher.text(query, max_results=max_results))


def _int_value(value: object, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = (
            value
            if isinstance(value, int) and not isinstance(value, bool)
            else int(str(value))
        )
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
    result: list[str] = []
    for item in value:
        domain = _normalized_text(item)
        if domain is not None:
            result.append(domain.lower())
    return result


def _matches_domains(url: str, domains: list[str]) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return any(
        hostname == domain or hostname.endswith(f".{domain}") for domain in domains
    )


def _normalized_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
