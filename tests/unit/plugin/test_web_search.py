from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from typing import Any

import pytest
from ddgs.exceptions import DDGSException, TimeoutException

from toolang.base.errors import ToolangError
from toolang.base.types.tool import ToolContext
from toolang.plugin.toolsets.web import create_toolset


@pytest.mark.parametrize(
    ("config", "expected_backend"),
    [
        ({}, "brave"),
        ({"backend": "duckduckgo"}, "duckduckgo"),
        ({"backend": "auto"}, "auto"),
        ({"backend": "google,brave"}, "google,brave"),
    ],
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
        "query": "toolang site:example.com",
        "max_results": 15,
        "backend": expected_backend,
    }
    assert {key: result[key] for key in ("query", "domains", "results")} == {
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


@pytest.mark.parametrize(
    "first_outcome",
    [
        DDGSException("No results found."),
        TimeoutException("request timed out"),
        [],
        [{"href": "https://elsewhere.test/page"}],
        [{"href": "https://[invalid"}, {"href": "javascript:void(0)"}, {}],
    ],
)
def test_web_search_falls_back_until_results_match_domains(
    monkeypatch, tmp_path, first_outcome
) -> None:
    calls = []

    async def search(query, *, max_results, timeout, backend):
        calls.append((query, backend))
        if len(calls) == 1:
            if isinstance(first_outcome, Exception):
                raise first_outcome
            return first_outcome
        return [{"href": "https://docs.fly.io/machines", "title": "Machines"}]

    monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", search)
    result = asyncio.run(
        create_toolset({})
        .tools()["search"]
        .invoke(
            {"query": "Fly.io Machines", "domains": ["FLY.IO"]},
            ToolContext(tmp_path, tmp_path),
        )
    ).output

    assert calls == [
        ("Fly.io Machines site:fly.io", "brave"),
        ("Fly.io Machines site:fly.io", "yahoo"),
    ]
    assert {key: result[key] for key in ("query", "domains", "results")} == {
        "query": "Fly.io Machines",
        "domains": ["fly.io"],
        "results": [
            {
                "title": "Machines",
                "url": "https://docs.fly.io/machines",
                "snippet": None,
            }
        ],
    }


def test_web_search_reports_unavailable_services_after_all_backends_fail(
    monkeypatch, tmp_path, caplog
) -> None:
    calls = []

    async def search(query, *, max_results, timeout, backend):
        calls.append(backend)
        if backend == "google":
            return []
        raise DDGSException("request failed")

    monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", search)
    with caplog.at_level("DEBUG", logger="toolang.plugin.toolsets.web"):
        result = asyncio.run(
            create_toolset({})
            .tools()["search"]
            .invoke({"query": "Fly.io"}, ToolContext(tmp_path, tmp_path))
        )
    assert result.error and "no usable results" in result.error
    assert result.output["status"] == "unavailable"
    assert [item["status"] for item in result.output["attempts"]] == [
        "error",
        "error",
        "empty",
        "error",
    ]
    assert "mojeek" in result.output["guidance"]
    assert "does not establish" in result.output["guidance"]

    assert calls == ["brave", "yahoo", "google", "duckduckgo"]
    for backend in calls:
        assert backend in caplog.text
    assert "request failed" in caplog.text
    assert "elapsed_ms" in caplog.text


def test_web_search_retries_within_total_timeout(monkeypatch, tmp_path) -> None:
    calls = []

    async def search(query, *, max_results, timeout, backend):
        calls.append(backend)
        if len(calls) == 1:
            raise DDGSException("No results found.")
        await asyncio.Event().wait()

    monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", search)
    result = asyncio.run(
        create_toolset({"timeout": 1})
        .tools()["search"]
        .invoke({"query": "Fly.io"}, ToolContext(tmp_path, tmp_path))
    )
    assert result.error == "web search timed out after 1s"
    assert result.output["status"] == "timeout"
    assert result.output["attempts"][-1]["status"] == "timeout"
    assert calls == ["brave", "yahoo"]


def test_web_search_uses_last_backend_when_earlier_backends_fail(
    monkeypatch, tmp_path
) -> None:
    calls = []

    async def search(query, *, max_results, timeout, backend):
        assert query == "Fly.io"
        calls.append(backend)
        if backend != "duckduckgo":
            raise DDGSException("request failed")
        return [{"href": "https://fly.io/docs/"}]

    monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", search)
    result = asyncio.run(
        create_toolset({})
        .tools()["search"]
        .invoke({"query": "Fly.io"}, ToolContext(tmp_path, tmp_path))
    ).output

    assert calls == ["brave", "yahoo", "google", "duckduckgo"]
    assert result["results"][0]["url"] == "https://fly.io/docs/"


def test_web_search_honors_explicit_backend_on_failure(monkeypatch, tmp_path) -> None:
    calls = []

    async def search(query, *, max_results, timeout, backend):
        calls.append(backend)
        raise DDGSException("request failed")

    monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", search)
    result = asyncio.run(
        create_toolset({"backend": " yahoo "})
        .tools()["search"]
        .invoke({"query": "Fly.io"}, ToolContext(tmp_path, tmp_path))
    )
    assert result.error and "no usable results" in result.error
    assert "preferred_backend" not in result.output["guidance"]
    assert calls == ["yahoo"]


def test_web_search_scopes_multiple_domains_and_filters_results(
    monkeypatch, tmp_path
) -> None:
    async def search(query, *, max_results, timeout, backend):
        assert query == "deployment (site:fly.io OR site:example.com)"
        assert max_results == 6
        return [
            {"href": "https://fly.io.evil.test/"},
            {"href": "https://notfly.io/"},
            {"href": "https://example.com@evil.test/"},
            {"href": "https://fly.io/docs/", "title": " Fly.io "},
            {"href": "https://docs.example.com/"},
            {"href": "https://example.com/extra"},
        ]

    monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", search)
    result = asyncio.run(
        create_toolset({})
        .tools()["search"]
        .invoke(
            {
                "query": "deployment",
                "domains": [" FLY.IO ", "example.com"],
                "top_k": 2,
            },
            ToolContext(tmp_path, tmp_path),
        )
    ).output

    assert result["query"] == "deployment"
    assert result["domains"] == ["fly.io", "example.com"]
    assert result["results"] == [
        {"url": "https://fly.io/docs/", "title": "Fly.io", "snippet": None},
        {"url": "https://docs.example.com/", "title": None, "snippet": None},
    ]


def test_web_search_cancels_stalled_attempt_before_fallback(monkeypatch, tmp_path):
    calls = []
    cancelled = []

    async def search(query, *, max_results, timeout, backend):
        calls.append(backend)
        if backend == "brave":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(backend)
        assert cancelled == ["brave"]
        return [{"href": "https://fly.io/"}]

    monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", search)
    monkeypatch.setattr("toolang.plugin.toolsets.web.BACKEND_TIMEOUT", 0.01)
    result = asyncio.run(
        create_toolset({})
        .tools()["search"]
        .invoke({"query": "Fly.io"}, ToolContext(tmp_path, tmp_path))
    ).output

    assert calls == ["brave", "yahoo"]
    assert result["results"][0]["url"] == "https://fly.io/"


def test_web_search_propagates_cancellation_without_fallback(monkeypatch, tmp_path):
    calls = []

    async def scenario():
        started = asyncio.Event()

        async def search(query, *, max_results, timeout, backend):
            calls.append(backend)
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", search)
        task = asyncio.create_task(
            create_toolset({})
            .tools()["search"]
            .invoke({"query": "Fly.io"}, ToolContext(tmp_path, tmp_path))
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert calls == ["brave"]


def test_web_search_does_not_hide_programming_errors(monkeypatch, tmp_path):
    calls = []

    async def search(query, *, max_results, timeout, backend):
        calls.append(backend)
        raise ValueError("unexpected bug")

    monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", search)
    with pytest.raises(ValueError, match="unexpected bug"):
        asyncio.run(
            create_toolset({})
            .tools()["search"]
            .invoke({"query": "Fly.io"}, ToolContext(tmp_path, tmp_path))
        )
    assert calls == ["brave"]


@pytest.mark.parametrize("backend", ["google", "auto", "google,brave"])
def test_web_search_explicit_backend_keeps_the_total_timeout(
    monkeypatch, tmp_path, backend
) -> None:
    calls = []

    async def search(query, *, max_results, timeout, backend):
        calls.append(backend)
        # An explicit DDGS expression can take several requests to complete.
        await asyncio.sleep(0.02)
        return [{"href": "https://fly.io/docs/"}]

    monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", search)
    monkeypatch.setattr("toolang.plugin.toolsets.web.BACKEND_TIMEOUT", 0.01)
    result = asyncio.run(
        create_toolset({"backend": backend, "timeout": 15})
        .tools()["search"]
        .invoke({"query": "Fly.io"}, ToolContext(tmp_path, tmp_path))
    ).output

    assert calls == [backend]
    assert result["results"][0]["url"] == "https://fly.io/docs/"


@pytest.mark.parametrize(
    "href",
    [
        "https://fly.io:invalid/docs/",
        "https://fly.io:999999/docs/",
        "https://bad host/docs/",
        "https://fly.io/do cs/",
    ],
)
def test_web_search_does_not_stop_on_malformed_result_urls(
    monkeypatch, tmp_path, href
) -> None:
    calls = []

    async def search(query, *, max_results, timeout, backend):
        calls.append(backend)
        if backend == "brave":
            return [{"href": href}]
        return [{"href": "https://fly.io:443/docs/a%20b"}]

    monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", search)
    result = asyncio.run(
        create_toolset({})
        .tools()["search"]
        .invoke({"query": "Fly.io"}, ToolContext(tmp_path, tmp_path))
    ).output

    assert calls == ["brave", "yahoo"]
    assert result["results"][0]["url"] == "https://fly.io:443/docs/a%20b"


@pytest.mark.parametrize(
    "preferred", ["brave", "google", "duckduckgo", "yahoo", "mojeek", "startpage"]
)
def test_web_search_preference_reorders_without_disabling_fallback(
    monkeypatch, tmp_path, preferred
):
    calls = []

    async def search(query, *, max_results, timeout, backend):
        calls.append(backend)
        raise DDGSException("No results found.")

    monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", search)
    result = asyncio.run(
        create_toolset({})
        .tools()["search"]
        .invoke(
            {"query": "Fly.io", "preferred_backend": preferred},
            ToolContext(tmp_path, tmp_path),
        )
    )
    assert calls == [
        preferred,
        *(
            name
            for name in ("brave", "yahoo", "google", "duckduckgo")
            if name != preferred
        ),
    ]
    assert result.error
    assert [attempt["backend"] for attempt in result.output["attempts"]] == calls
    assert all(attempt["elapsed_ms"] >= 0 for attempt in result.output["attempts"])
    assert all(attempt["status"] == "error" for attempt in result.output["attempts"])


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": ""},
        {"query": "  "},
        {"query": None},
        {"query": "Fly.io", "preferred_backend": "auto"},
        {"query": "Fly.io", "preferred_backend": "brave,google"},
        {"query": "Fly.io", "preferred_backend": "unknown"},
        {"query": "Fly.io", "domains": ["https://fly.io/docs/"]},
        {"query": "Fly.io", "domains": ["fly.io OR site:evil.test"]},
        {"query": "Fly.io", "domains": [None]},
    ],
)
def test_web_search_rejects_invalid_arguments_before_search(
    monkeypatch, tmp_path, arguments
):
    async def unexpected(*args, **kwargs):
        pytest.fail("invalid input must not reach a provider")

    monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", unexpected)
    with pytest.raises(ToolangError):
        asyncio.run(
            create_toolset({})
            .tools()["search"]
            .invoke(arguments, ToolContext(tmp_path, tmp_path))
        )


def test_web_search_preference_cannot_override_plugin_configuration(tmp_path):
    tool = create_toolset({"backend": "google"}).tools()["search"]
    assert tool.definition().parameters["properties"]["preferred_backend"]["enum"] == [
        None
    ]
    with pytest.raises(ToolangError, match="cannot override"):
        asyncio.run(
            tool.invoke(
                {"query": "Fly.io", "preferred_backend": "brave"},
                ToolContext(tmp_path, tmp_path),
            )
        )


def test_web_search_schema_and_guidance_are_model_visible():
    definition = create_toolset({"top_k": 7}).tools()["search"].definition()
    properties = definition.parameters["properties"]
    assert definition.parameters["required"] == ["query"]
    assert definition.parameters["additionalProperties"] is False
    assert properties["query"]["type"] == "string"
    assert properties["top_k"]["type"] == "integer"
    assert properties["top_k"]["default"] == 7
    assert properties["domains"]["items"]["type"] == "string"
    assert properties["preferred_backend"]["default"] is None
    assert "yahoo" in properties["preferred_backend"]["enum"]
    assert all(properties[name]["description"] for name in properties)
    assert "not full-page verification" in definition.description
    assert "same failed call immediately" in definition.description


def test_web_search_reports_successful_provider_and_normalizes_domains(
    monkeypatch, tmp_path
):
    async def search(query, *, max_results, timeout, backend):
        assert backend == "yahoo"
        assert query == "Fly.io site:fly.io"
        return [{"href": "https://docs.fly.io./machines"}]

    monkeypatch.setattr("toolang.plugin.toolsets.web._run_search", search)
    result = asyncio.run(
        create_toolset({})
        .tools()["search"]
        .invoke(
            {
                "query": "Fly.io",
                "preferred_backend": "yahoo",
                "domains": ["FLY.IO.", "fly.io"],
            },
            ToolContext(tmp_path, tmp_path),
        )
    )
    assert result.error is None
    assert result.output["backend"] == "yahoo"
    assert result.output["status"] == "ok"
    assert result.output["domains"] == ["fly.io"]
    assert len(result.output["attempts"]) == 1
    assert result.output["attempts"][0]["result_count"] == 1


@pytest.mark.parametrize(
    "classes", ["dd algo algo-sr", "dd algo algo-sr relsrch Sr", "relsrch"]
)
def test_yahoo_adapter_parses_both_layouts_without_duplicate_links(classes):
    from toolang.plugin.toolsets._web_yahoo import _YahooSearch

    page = f'''
    <html><body>
      <h3><a href="https://unrelated.test/">Navigation</a></h3>
      <div class="{classes}">
        <div class="compTitle"><h3><a href="https://r.search.yahoo.com/path/RU=https%3A%2F%2Ffly.io%2Fdocs%2F%3Ffoo%3D1%26bar%3D2/RK=2/RS=signature">
          <span class="title-url">fly.io › docs</span>Fly <b>Machines</b> docs
        </a></h3><a href="https://favicon.test/">Icon</a></div>
        <div class="compText"><p>Deploy <b>Machines</b> safely.</p></div>
      </div>
      <div class="{classes}"><div class="compTitle"><h3>
        <a href="https://www.bing.com/aclick?ad=1">Advertisement</a>
      </h3></div></div>
    </body></html>
    '''
    engine = _YahooSearch(timeout=1)
    results = engine.post_extract_results(engine.extract_results(page))
    assert len(results) == 1
    assert results[0].href == "https://fly.io/docs/?foo=1&bar=2"
    assert results[0].title == "Fly Machines docs"
    assert results[0].body == "Deploy Machines safely."


def test_yahoo_adapter_uses_instance_local_compatibility(monkeypatch):
    from ddgs.engines import ENGINES
    from ddgs.engines.yahoo import Yahoo
    from ddgs.results import TextResult
    from toolang.plugin.toolsets._web_yahoo import _YahooSearch, search_yahoo
    from toolang.plugin.toolsets.web import _search_text

    calls = []

    def search(self, query, **kwargs):
        calls.append(query)
        return [TextResult(title="Fly docs", href="https://fly.io/docs/")]

    monkeypatch.setattr(_YahooSearch, "search", search)
    assert _search_text("Fly.io", 1, 5, "yahoo") == search_yahoo("Fly.io", 1, 5)
    assert calls == ["Fly.io", "Fly.io"]
    assert ENGINES["text"]["yahoo"] is Yahoo
