"""Tavily search toolset."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import os
from typing import Any

import httpx

from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import ToolResult
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.base.utils.tool_descriptions import action_summary


@dataclass(slots=True)
class TavilyToolset:
    """Expose Tavily search without any registration-time availability checks."""

    timeout: float = 15
    api_key: str | None = field(default=None, repr=False)
    name: str = "tavily"
    description: str | None = "Find web sources and content excerpts with Tavily."
    _tools: dict[str, Tool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        @tool(
            name="search",
            summary=_summary,
            description=(
                "Find current web information with Tavily basic search. "
                "Returns JSON results with titles, URLs, content excerpts, and "
                "relevance scores. max_results accepts 1-20 (default 5); "
                "include_domains restricts results to the listed domains. "
                "Works without an API key using rate-limited keyless access."
            ),
        )
        async def search(
            query: str,
            max_results: int = 5,
            include_domains: list[str] | None = None,
        ) -> ToolResult:
            if not query.strip():
                return ToolResult(error="query must not be blank")
            if not 1 <= max_results <= 20:
                return ToolResult(error="max_results must be between 1 and 20")
            headers = (
                {"Authorization": f"Bearer {self.api_key}"}
                if self.api_key
                else {"X-Tavily-Access-Mode": "keyless"}
            )
            body: dict[str, Any] = {
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
                "auto_parameters": False,
            }
            if include_domains is not None:
                body["include_domains"] = include_domains
            try:
                async with asyncio.timeout(self.timeout):
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        response = await client.post(
                            "https://api.tavily.com/search",
                            headers=headers,
                            json=body,
                        )
            except (TimeoutError, httpx.TimeoutException):
                return ToolResult(error="Tavily request timed out")
            except httpx.RequestError:
                return ToolResult(error="Tavily network request failed")
            try:
                payload = response.json()
            except ValueError:
                if response.is_error:
                    return ToolResult(error=f"Tavily HTTP {response.status_code}")
                return ToolResult(error="Tavily returned invalid JSON")
            if not isinstance(payload, dict):
                return ToolResult(error="Tavily returned a non-object response")
            output = _redact(payload, self.api_key)
            if response.is_error:
                return ToolResult(output, error=f"Tavily HTTP {response.status_code}")
            if output.get("error"):
                return ToolResult(output, error="Tavily reported a search error")
            return ToolResult(output)

        self._tools = {"search": create_function_tool(search)}

    def tools(self) -> Mapping[str, Tool]:
        return dict(self._tools)


def _redact(value: Any, secret: str | None) -> Any:
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    return (
        value.replace(secret, "[REDACTED]")
        if secret and isinstance(value, str)
        else value
    )


def _summary(
    arguments: Mapping[str, Any], result: ToolResult | None = None
) -> str | None:
    query = arguments.get("query")
    if not isinstance(query, str):
        return None
    return action_summary(result, ("search:", "Searching:", "Searched:"), f"“{query}”")


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Create a normal toolset using the selected workload environment."""

    if "api_key" in config:
        raise ValueError("use api_key_env to reference a credential variable")
    raw_timeout = config.get("timeout", 15)
    if isinstance(raw_timeout, bool):
        raise ValueError("timeout must be a positive finite number")
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("timeout must be a positive finite number") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    key_name = config.get("api_key_env", "TAVILY_API_KEY")
    if not isinstance(key_name, str) or not key_name.strip():
        raise ValueError("api_key_env must be a non-empty environment variable name")
    return TavilyToolset(
        timeout=timeout,
        api_key=os.environ.get(key_name, "").strip() or None,
    )
