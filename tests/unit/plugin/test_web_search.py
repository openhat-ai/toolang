from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from typing import Any

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.tool import ToolContext
from toolang.plugin.toolsets.web import create_toolset


@pytest.mark.parametrize(
    ("config", "expected_backend"),
    [({}, "google"), ({"backend": "duckduckgo"}, "duckduckgo")],
)
def test_web_search_uses_plugin_backend(
    monkeypatch,
    tmp_path,
    config: dict[str, Any],
    expected_backend: str,
) -> None:
    calls: dict[str, Any] = {}

    class FakeDDGS:
        def __init__(self, *, timeout: int) -> None:
            calls["timeout"] = timeout

        def __enter__(self) -> FakeDDGS:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def text(
            self,
            query: str,
            *,
            max_results: int,
            backend: str,
        ) -> list[dict[str, str]]:
            calls["query"] = query
            calls["max_results"] = max_results
            calls["backend"] = backend
            return [
                {
                    "href": "https://example.com/page",
                    "title": "Example",
                    "body": "A result.",
                }
            ]

    ddgs_module = ModuleType("ddgs")
    ddgs_module.DDGS = FakeDDGS  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ddgs", ddgs_module)

    async def run_sync(function, *args, cancellable: bool):
        assert cancellable is True
        return function(*args)

    monkeypatch.setattr(
        "toolang.plugin.toolsets.web.to_process.run_sync",
        run_sync,
    )
    tool = create_toolset(config).tools()["search"]

    result = asyncio.run(
        tool.invoke(
            {"query": "toolang", "domains": ["example.com"]},
            ToolContext(tmp_path, tmp_path),
        )
    ).output

    assert calls == {
        "timeout": 5,
        "query": "toolang",
        "max_results": 15,
        "backend": expected_backend,
    }
    assert result == {
        "query": "toolang",
        "domains": ["example.com"],
        "results": [
            {
                "title": "Example",
                "url": "https://example.com/page",
                "snippet": "A result.",
            }
        ],
    }


def test_web_search_rejects_invalid_backend_config() -> None:
    with pytest.raises(ToolangError, match="web backend must be a non-empty string"):
        create_toolset({"backend": " "})
