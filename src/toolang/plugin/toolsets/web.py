"""Web toolset plugin."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from anyio import to_process
from ddgs.exceptions import DDGSException, TimeoutException

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import ToolResult
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.base.utils.tool_descriptions import action_summary

logger = logging.getLogger(__name__)

DEFAULT_BACKENDS = ("brave", "yahoo", "google", "duckduckgo")
PREFERRED_BACKENDS = (*DEFAULT_BACKENDS, "mojeek", "startpage")
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
        pinned = self.config.get("backend") is not None

        @tool(
            name="search",
            description=(
                "Find public web pages and return URLs, titles, and snippets. "
                "Use focused search terms; domains restrict results to those hostnames "
                "and their subdomains. Leave preferred_backend unset normally: "
                "the tool automatically tries independent providers within a total "
                "time budget. Use a preference to try an untried provider or seek "
                "another source after inspecting attempts. Snippets are discovery "
                "evidence, not full-page verification. On failure, inspect attempts "
                "and guidance; do not infer that no relevant pages exist or repeat "
                "the same failed call immediately."
            ),
            parameters=_search_parameters(self._top_k, pinned=pinned),
            summary=_summary,
        )
        async def search(
            query: str,
            top_k: int = self._top_k,
            domains: list[str] | None = None,
            preferred_backend: str | None = None,
        ) -> ToolResult:
            if not isinstance(query, str) or not query.strip():
                raise ToolangError("web query must be a non-empty string")
            limit = _int_value(top_k, default=self._top_k)
            normalized_domains = _domains(domains)
            backends = _preferred_backends(
                self._backends, preferred_backend, pinned=pinned
            )
            search_query = _domain_query(query, normalized_domains)
            attempt_timeout = BACKEND_TIMEOUT if len(backends) > 1 else None
            attempts: list[dict[str, Any]] = []
            output: dict[str, Any] = {
                "query": query,
                "domains": normalized_domains,
                "results": [],
                "status": "unavailable",
                "backend": None,
                "attempts": attempts,
            }
            started = monotonic()
            try:
                async with asyncio.timeout(self._timeout):
                    for backend in backends:
                        started = monotonic()
                        attempt: dict[str, Any] = {"backend": backend}
                        attempts.append(attempt)
                        try:
                            async with asyncio.timeout(attempt_timeout):
                                raw_results = await _run_search(
                                    search_query,
                                    max_results=limit * 3,
                                    timeout=min(self._timeout, BACKEND_TIMEOUT),
                                    backend=backend,
                                )
                        except (DDGSException, TimeoutError) as exc:
                            attempt.update(
                                status=(
                                    "timeout"
                                    if isinstance(exc, (TimeoutError, TimeoutException))
                                    else "error"
                                ),
                                error=str(exc)[:240] or type(exc).__name__,
                            )
                        else:
                            filtered = _filter_results(
                                raw_results, normalized_domains, limit
                            )
                            attempt.update(
                                status=(
                                    "ok"
                                    if filtered
                                    else "filtered_out"
                                    if raw_results
                                    else "empty"
                                ),
                                result_count=len(filtered),
                            )
                            if filtered:
                                output.update(
                                    results=filtered, status="ok", backend=backend
                                )
                        finally:
                            attempt["elapsed_ms"] = round(
                                (monotonic() - started) * 1000
                            )
                            logger.debug("web.search.attempt %s", attempt)
                        if output["results"]:
                            return ToolResult(output)
            except TimeoutError:
                if attempts:
                    attempts[-1].update(
                        status="timeout", error="total search time budget exhausted"
                    )
                output["status"] = "timeout"
                error = f"web search timed out after {self._timeout}s"
            else:
                error = (
                    "web search services returned no usable results "
                    f"(tried: {', '.join(backends)})"
                )
            output["guidance"] = _recovery_guidance(attempts, pinned=pinned)
            return ToolResult(output, error=error)

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
    if backend == "yahoo":
        from toolang.plugin.toolsets._web_yahoo import search_yahoo

        return search_yahoo(query, max_results, timeout)
    with DDGS(timeout=timeout) as searcher:
        return list(searcher.text(query, max_results=max_results, backend=backend))


def _backend_values(value: object) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_BACKENDS
    if not isinstance(value, str) or not value.strip():
        raise ToolangError("web backend must be a non-empty string")
    # Explicit DDGS expressions (including auto and lists) remain opt-in.
    return (value.strip(),)


def _search_parameters(top_k: int, *, pinned: bool) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Focused search terms in the language of the desired sources.",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "default": top_k,
                "description": "Maximum results to return; fewer may be available.",
            },
            "domains": {
                "type": ["array", "null"],
                "items": {"type": "string", "minLength": 1},
                "default": None,
                "description": (
                    'Optional hostnames, e.g. ["docs.python.org"], not URLs or paths. '
                    "Includes subdomains. Omit to search the whole public web."
                ),
            },
            "preferred_backend": {
                "type": ["string", "null"],
                "enum": [None] if pinned else [None, *PREFERRED_BACKENDS],
                "default": None,
                "description": (
                    "Leave unset: the backend is fixed by plugin configuration."
                    if pinned
                    else "Optional provider to try first; automatic fallback remains enabled. "
                    "Normally omit. After failure, prefer an engine not listed in attempts."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }


def _preferred_backends(
    backends: tuple[str, ...], preference: object, *, pinned: bool
) -> tuple[str, ...]:
    if preference is None:
        return backends
    if pinned:
        raise ToolangError(
            "preferred_backend cannot override plugin backend configuration"
        )
    if not isinstance(preference, str) or preference not in PREFERRED_BACKENDS:
        raise ToolangError(
            f"preferred_backend must be one of: {', '.join(PREFERRED_BACKENDS)}"
        )
    return (preference, *(backend for backend in backends if backend != preference))


def _recovery_guidance(attempts: list[dict[str, Any]], *, pinned: bool) -> str:
    guidance = (
        "No usable search results were obtained; this does not establish that no "
        "relevant pages exist. Do not repeat an identical failed call immediately. "
    )
    if any(attempt.get("status") in {"empty", "filtered_out"} for attempt in attempts):
        guidance += "Try a more focused query or broaden domains if appropriate. "
    remaining = [
        backend
        for backend in PREFERRED_BACKENDS
        if backend not in {attempt["backend"] for attempt in attempts}
    ]
    if remaining and not pinned:
        guidance += f"Untried preferred_backend options: {', '.join(remaining)}. "
    return (
        guidance
        + "If providers remain unavailable, report the limitation and retry later."
    )


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
        if not isinstance(item, str) or not item.strip():
            raise ToolangError("web domains must contain non-empty hostnames")
        try:
            domain = item.strip().rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ToolangError("web domains must contain valid hostnames") from exc
        if len(domain) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in domain.split(".")
        ):
            raise ToolangError(
                "web domains must be hostnames such as example.com, not URLs or paths"
            )
        if domain not in result:
            result.append(domain)
    return result


def _matches_domains(url: str, domains: list[str]) -> bool:
    if any(character.isspace() for character in url):
        return False
    try:
        parsed = urlparse(url)
        # urlparse defers invalid-port errors until this property is accessed.
        _ = parsed.port
        hostname = (
            (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        )
    except (ValueError, UnicodeError):
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
