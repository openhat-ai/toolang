"""Web toolset plugin."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from anyio import to_process
from ddgs.exceptions import DDGSException

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import ToolResult
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.base.utils.tool_descriptions import action_summary

logger = logging.getLogger(__name__)

DEFAULT_BACKENDS = ("brave", "google", "duckduckgo")
DEFAULT_TOP_K = 5
DEFAULT_TIMEOUT = 15
BACKEND_TIMEOUT = 5


@dataclass(slots=True)
class WebToolset:
    """Public-web search tools."""

    config: dict[str, Any]
    name: str = "web"
    description: str | None = (
        "Search the public web and return concise result snippets."
    )
    _backends: tuple[str, ...] = field(init=False, repr=False)
    _top_k: int = field(init=False, repr=False)
    _timeout: int = field(init=False, repr=False)
    _tools: dict[str, Tool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._backends = _backend_values(self.config.get("backend"))
        self._top_k = _int_value(self.config.get("top_k"), default=DEFAULT_TOP_K)
        self._timeout = _int_value(
            self.config.get("timeout"),
            default=DEFAULT_TIMEOUT,
        )
        self._tools = self._build_tools()

    def tools(self) -> Mapping[str, Tool]:
        return dict(self._tools)

    def _build_tools(self) -> dict[str, Tool]:
        @tool(name="search", description="Search the public web.", summary=_summary)
        async def search(
            query: str,
            top_k: int = self._top_k,
            domains: list[str] | None = None,
        ) -> dict[str, Any]:
            limit = _int_value(top_k, default=self._top_k)
            normalized_domains = _domains(domains)
            search_query = _domain_query(query, normalized_domains)
            attempt_timeout = min(self._timeout, BACKEND_TIMEOUT)
            try:
                async with asyncio.timeout(self._timeout):
                    for backend in self._backends:
                        started = monotonic()
                        try:
                            async with asyncio.timeout(attempt_timeout):
                                raw_results = await _run_search(
                                    search_query,
                                    max_results=limit * 3,
                                    timeout=attempt_timeout,
                                    backend=backend,
                                )
                        except (DDGSException, TimeoutError) as exc:
                            logger.debug(
                                "web.search.failed backend=%s elapsed=%.3f error=%r",
                                backend,
                                monotonic() - started,
                                exc,
                            )
                            continue
                        filtered = _filter_results(
                            raw_results, normalized_domains, limit
                        )
                        logger.debug(
                            "web.search.results backend=%s elapsed=%.3f raw=%d usable=%d",
                            backend,
                            monotonic() - started,
                            len(raw_results),
                            len(filtered),
                        )
                        if filtered:
                            return {
                                "query": query,
                                "domains": normalized_domains,
                                "results": filtered,
                            }
            except TimeoutError as exc:
                raise ToolangError(
                    f"web search timed out after {self._timeout}s"
                ) from exc
            raise ToolangError(
                "web search services returned no usable results "
                f"(tried: {', '.join(self._backends)})"
            )

        return {"search": create_function_tool(search)}


def _summary(
    arguments: Mapping[str, Any],
    result: ToolResult | None = None,
) -> str | None:
    query = arguments.get("query")
    if not isinstance(query, str):
        return None
    return action_summary(
        result, ("search for", "Searching for", "Searched for"), f"“{query}”"
    )


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Create the web toolset plugin."""

    return WebToolset(config=dict(config))


async def _run_search(
    query: str,
    *,
    max_results: int,
    timeout: int,
    backend: str,
) -> list[dict[str, Any]]:
    return await to_process.run_sync(
        _search_text,
        query,
        max_results,
        timeout,
        backend,
        cancellable=True,
    )


def _search_text(
    query: str,
    max_results: int,
    timeout: int,
    backend: str,
) -> list[dict[str, Any]]:
    try:
        from ddgs import DDGS
    except ImportError as exc:  # pragma: no cover
        raise ToolangError(
            "The 'ddgs' package is not installed. Install Toolang dependencies to enable web."
        ) from exc
    with DDGS(timeout=timeout) as searcher:
        return list(searcher.text(query, max_results=max_results, backend=backend))


def _backend_values(value: object) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_BACKENDS
    if not isinstance(value, str) or not value.strip():
        raise ToolangError("web backend must be a non-empty string")
    # Explicit DDGS expressions (including auto and lists) remain opt-in.
    return (value.strip(),)


def _domain_query(query: str, domains: list[str]) -> str:
    if not domains:
        return query
    sites = " OR ".join(f"site:{domain}" for domain in domains)
    if len(domains) > 1:
        sites = f"({sites})"
    return f"{query} {sites}"


def _filter_results(
    raw_results: list[dict[str, Any]], domains: list[str], limit: int
) -> list[dict[str, str | None]]:
    filtered: list[dict[str, str | None]] = []
    for item in raw_results:
        href = _normalized_text(item.get("href"))
        if href is None or not _matches_domains(href, domains):
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
    return filtered


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
        raise ToolangError("web integer argument is invalid") from exc
    if parsed <= 0:
        raise ToolangError("web integer argument must be positive")
    return parsed


def _domains(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ToolangError("web domains must be a list of hostnames")
    result: list[str] = []
    for item in value:
        domain = _normalized_text(item)
        if domain is not None:
            result.append(domain.lower())
    return result


def _matches_domains(url: str, domains: list[str]) -> bool:
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False
    return not domains or any(
        hostname == domain or hostname.endswith(f".{domain}") for domain in domains
    )


def _normalized_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
