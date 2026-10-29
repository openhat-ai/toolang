"""Independent built-in Tavily and SerpAPI search tools."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from toolang.base.protocols.tool import Tool
from toolang.base.types.tool import ToolContext
from toolang.plugin.toolsets.loading import LoadedTool, load_tools

from toolang.plugin.toolsets.serpapi import create_toolset as create_serpapi
from toolang.plugin.toolsets.tavily import create_toolset as create_tavily


@pytest.fixture
def context(tmp_path):
    return ToolContext(tmp_path, tmp_path)


@pytest.fixture(autouse=True)
def no_provider_credentials(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)


def install_transport(monkeypatch, handler):
    client_class = httpx.AsyncClient
    clients = []

    def make_client(**kwargs):
        client = client_class(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    return clients


def invoke(factory, arguments, context):
    return asyncio.run(factory({}).tools()["search"].invoke(arguments, context))


def test_default_search_tools_are_exposed_without_credentials(monkeypatch):
    def no_network(**kwargs):
        pytest.fail("tool discovery must not contact a provider")

    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    tools = load_tools()
    assert {"tavily__search", "serpapi__search"} <= tools.keys()
    assert "web__search" not in tools
    for name in ("tavily", "serpapi"):
        tool = tools[f"{name}__search"]
        assert isinstance(tool, LoadedTool)
        assert isinstance(tool.leaf_tool, Tool)
        assert tool.source == "built-in"
        assert tool.public_name == f"{name}/search"


def test_normal_tool_selection_can_exclude_a_provider():
    selected = load_tools(
        toolsets=("tavily", "serpapi"),
        queries=("tavily/search",),
    )
    assert set(selected) == {"tavily__search"}


def test_providers_keep_different_public_schemas():
    tools = load_tools(toolsets=("tavily", "serpapi"))
    tavily = tools["tavily__search"].definition().parameters
    serpapi = tools["serpapi__search"].definition().parameters
    assert tavily["required"] == ["query"]
    assert serpapi["required"] == ["q"]
    assert "max_results" in tavily["properties"]
    assert "gl" in serpapi["properties"]
    assert "q" not in tavily["properties"]
    assert "query" not in serpapi["properties"]


def test_tavily_keyless_request_and_native_response(monkeypatch, context):
    payload = {
        "query": "Python",
        "results": [{"content": "Native excerpt", "score": 0.9}],
    }

    def handler(request):
        assert str(request.url) == "https://api.tavily.com/search"
        assert request.headers["X-Tavily-Access-Mode"] == "keyless"
        assert "Authorization" not in request.headers
        body = json.loads(request.content)
        assert body == {
            "query": "Python",
            "max_results": 3,
            "include_domains": ["docs.python.org"],
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "auto_parameters": False,
        }
        return httpx.Response(200, json=payload)

    clients = install_transport(monkeypatch, handler)
    result = invoke(
        create_tavily,
        {"query": "Python", "max_results": 3, "include_domains": ["docs.python.org"]},
        context,
    )
    assert result.error is None
    assert result.output == payload
    assert clients[0].is_closed


def test_tavily_uses_configured_environment_reference(monkeypatch, context):
    monkeypatch.setenv("TRIAL_TAVILY_KEY", "private-trial-token")

    def handler(request):
        assert request.headers["Authorization"] == "Bearer private-trial-token"
        assert "X-Tavily-Access-Mode" not in request.headers
        return httpx.Response(200, json={"results": []})

    install_transport(monkeypatch, handler)
    tools = load_tools(
        toolsets=("tavily",),
        toolset_config={"tavily": {"api_key_env": "TRIAL_TAVILY_KEY"}},
    )
    result = asyncio.run(tools["tavily__search"].invoke({"query": "Python"}, context))
    assert result.error is None


def test_serpapi_stays_exposed_without_credentials(monkeypatch, context):
    def no_network(**kwargs):
        pytest.fail("missing credentials are an invocation error")

    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    tools = load_tools(toolsets=("serpapi",))
    assert set(tools) == {"serpapi__search"}
    result = asyncio.run(tools["serpapi__search"].invoke({"q": "Python"}, context))
    assert result.error == "SerpAPI requires an API key when invoked"


def test_serpapi_native_parameters_and_response(monkeypatch, context):
    monkeypatch.setenv("SERPAPI_API_KEY", "private-trial-token")
    payload = {
        "search_metadata": {"status": "Success"},
        "organic_results": [{"title": "Python", "link": "https://python.org"}],
    }

    def handler(request):
        assert request.url.host == "serpapi.com"
        assert request.url.path == "/search.json"
        assert dict(request.url.params) == {
            "engine": "google",
            "q": "Python",
            "gl": "us",
            "hl": "en",
            "api_key": "private-trial-token",
        }
        return httpx.Response(200, json=payload)

    clients = install_transport(monkeypatch, handler)
    result = invoke(create_serpapi, {"q": "Python", "gl": "us", "hl": "en"}, context)
    assert result.error is None
    assert result.output == payload
    assert clients[0].is_closed


@pytest.mark.parametrize(
    "factory,key_name,arguments",
    [
        (create_tavily, "TAVILY_API_KEY", {"query": "Python"}),
        (create_serpapi, "SERPAPI_API_KEY", {"q": "Python"}),
    ],
)
def test_provider_errors_do_not_leak_the_key(
    monkeypatch, context, factory, key_name, arguments
):
    secret = "private-trial-token"
    monkeypatch.setenv(key_name, secret)

    def handler(request):
        return httpx.Response(
            401,
            json={
                "error": f"Rejected {secret}",
                "details": [{"request_url": f"https://example.test/?api_key={secret}"}],
            },
        )

    install_transport(monkeypatch, handler)
    toolset = factory({})
    assert secret not in repr(toolset)
    result = asyncio.run(toolset.tools()["search"].invoke(arguments, context))
    assert "HTTP 401" in result.error
    encoded = json.dumps(result.output)
    assert secret not in encoded
    assert "[REDACTED]" in encoded


@pytest.mark.parametrize(
    "factory,key_name,arguments",
    [
        (create_tavily, "TAVILY_API_KEY", {"query": "Python"}),
        (create_serpapi, "SERPAPI_API_KEY", {"q": "Python"}),
    ],
)
def test_network_error_does_not_echo_credential_urls(
    monkeypatch, context, factory, key_name, arguments
):
    monkeypatch.setenv(key_name, "private-trial-token")

    def handler(request):
        raise httpx.ConnectError(f"Failed request: {request.url}", request=request)

    install_transport(monkeypatch, handler)
    result = invoke(factory, arguments, context)
    assert "network request failed" in result.error
    assert "private-trial-token" not in result.error
    assert result.output == {}


@pytest.mark.parametrize(
    "factory,arguments",
    [(create_tavily, {"query": "Python"}), (create_serpapi, {"q": "Python"})],
)
def test_http_timeout_reports_a_normal_tool_failure(
    monkeypatch, context, factory, arguments
):
    monkeypatch.setenv("SERPAPI_API_KEY", "private-trial-token")

    def handler(request):
        raise httpx.ReadTimeout("provider did not respond", request=request)

    clients = install_transport(monkeypatch, handler)
    result = invoke(factory, arguments, context)
    assert "timed out" in result.error
    assert clients[0].is_closed


@pytest.mark.parametrize(
    "factory,arguments",
    [(create_tavily, {"query": "Python"}), (create_serpapi, {"q": "Python"})],
)
def test_cancellation_closes_the_client(monkeypatch, context, factory, arguments):
    monkeypatch.setenv("SERPAPI_API_KEY", "private-trial-token")
    clients = []

    async def scenario():
        nonlocal clients
        entered = asyncio.Event()
        canceled = []

        async def handler(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                canceled.append(True)
                raise

        clients = install_transport(monkeypatch, handler)
        call = asyncio.create_task(
            factory({}).tools()["search"].invoke(arguments, context)
        )
        async with asyncio.timeout(2):
            await entered.wait()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        assert canceled == [True]

    asyncio.run(scenario())
    assert clients[0].is_closed


@pytest.mark.parametrize(
    "factory,arguments",
    [(create_tavily, {"query": "   "}), (create_serpapi, {"q": "   "})],
)
def test_blank_input_fails_without_a_network_call(
    monkeypatch, context, factory, arguments
):
    def no_network(**kwargs):
        pytest.fail("invalid input must not reach the provider")

    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    result = invoke(factory, arguments, context)
    assert "must not be blank" in result.error


@pytest.mark.parametrize(
    "factory,arguments",
    [(create_tavily, {"query": "Python"}), (create_serpapi, {"q": "Python"})],
)
def test_total_deadline_cancels_a_stalled_response(
    monkeypatch, context, factory, arguments
):
    monkeypatch.setenv("SERPAPI_API_KEY", "private-trial-token")
    canceled = []

    async def handler(request):
        try:
            await asyncio.Event().wait()
        finally:
            canceled.append(True)

    clients = install_transport(monkeypatch, handler)
    tool = factory({"timeout": 0.01}).tools()["search"]
    result = asyncio.run(tool.invoke(arguments, context))
    assert "timed out" in result.error
    assert canceled == [True]
    assert clients[0].is_closed


@pytest.mark.parametrize(
    "factory,arguments",
    [(create_tavily, {"query": "Python"}), (create_serpapi, {"q": "Python"})],
)
def test_provider_error_in_a_successful_http_response_is_a_failure(
    monkeypatch, context, factory, arguments
):
    monkeypatch.setenv("SERPAPI_API_KEY", "private-trial-token")
    install_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"error": "Search failed"}),
    )
    result = invoke(factory, arguments, context)
    assert result.error
    assert result.output == {"error": "Search failed"}


@pytest.mark.parametrize(
    "factory,arguments",
    [(create_tavily, {"query": "Python"}), (create_serpapi, {"q": "Python"})],
)
def test_non_json_http_errors_keep_the_status(monkeypatch, context, factory, arguments):
    monkeypatch.setenv("SERPAPI_API_KEY", "private-trial-token")
    install_transport(
        monkeypatch,
        lambda request: httpx.Response(403, text="<html>Access denied</html>"),
    )
    result = invoke(factory, arguments, context)
    assert "HTTP 403" in result.error
    assert result.output == {}


@pytest.mark.parametrize("factory", [create_tavily, create_serpapi])
def test_configuration_uses_credential_references(factory):
    with pytest.raises(ValueError, match="api_key_env"):
        factory({"api_key": "do-not-accept-literal-keys"})
    with pytest.raises(ValueError, match="api_key_env"):
        factory({"api_key_env": None})
    with pytest.raises(ValueError, match="positive finite"):
        factory({"timeout": float("inf")})


def test_tavily_result_limit_is_validated_before_request(monkeypatch, context):
    def no_network(**kwargs):
        pytest.fail("invalid result limits must not contact Tavily")

    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    result = invoke(create_tavily, {"query": "Python", "max_results": 21}, context)
    assert result.error == "max_results must be between 1 and 20"
