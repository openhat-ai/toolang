"""SerpAPI search toolset."""

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
class SerpAPIToolset:
    """Expose Google search even when no SerpAPI key has been supplied."""

    timeout: float = 15
    api_key: str | None = field(default=None, repr=False)
    name: str = "serpapi"
    description: str | None = "Search Google web results through SerpAPI."
    _tools: dict[str, Tool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        @tool(
            name="search",
            summary=_summary,
            description=(
                "Search Google web results through SerpAPI. q accepts search terms "
                "and Google operators such as site: and intitle:. Returns native "
                "JSON that may include organic_results, answer_box, and "
                "knowledge_graph. gl selects the country code (e.g. us); "
                "hl selects Google's language code (e.g. en). "
                "Requires a configured SerpAPI API key."
            ),
        )
        async def search(
            q: str,
            gl: str | None = None,
            hl: str | None = None,
        ) -> ToolResult:
            if not q.strip():
                return ToolResult(error="q must not be blank")
            if not self.api_key:
                return ToolResult(error="SerpAPI requires an API key when invoked")
            params = {"engine": "google", "q": q, "api_key": self.api_key}
            if gl is not None:
                params["gl"] = gl
            if hl is not None:
                params["hl"] = hl
            try:
                async with asyncio.timeout(self.timeout):
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        response = await client.get(
                            "https://serpapi.com/search.json", params=params
                        )
            except (TimeoutError, httpx.TimeoutException):
                return ToolResult(error="SerpAPI request timed out")
            except httpx.RequestError:
                return ToolResult(error="SerpAPI network request failed")
            try:
                payload = response.json()
            except ValueError:
                if response.is_error:
                    return ToolResult(error=f"SerpAPI HTTP {response.status_code}")
                return ToolResult(error="SerpAPI returned invalid JSON")
            if not isinstance(payload, dict):
                return ToolResult(error="SerpAPI returned a non-object response")
            output = _redact(payload, self.api_key)
            if response.is_error:
                return ToolResult(output, error=f"SerpAPI HTTP {response.status_code}")
            if output.get("error"):
                return ToolResult(output, error="SerpAPI reported a search error")
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
    query = arguments.get("q")
    if not isinstance(query, str):
        return None
    return action_summary(result, ("search:", "Searching:", "Searched:"), f"“{query}”")


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Create a normal toolset without checking API or credential availability."""

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
    key_name = config.get("api_key_env", "SERPAPI_API_KEY")
    if not isinstance(key_name, str) or not key_name.strip():
        raise ValueError("api_key_env must be a non-empty environment variable name")
    return SerpAPIToolset(
        timeout=timeout,
        api_key=os.environ.get(key_name, "").strip() or None,
    )
