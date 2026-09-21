from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging

import httpx
import pytest

from toolang.plugin.catalogs import llama_cpp as llama_cpp_models
from toolang.plugin.catalogs import ollama as ollama_models
from toolang.plugin.catalogs._local import LOCAL_ZERO_COST
from toolang.plugin.catalogs.llama_cpp import LlamaCppModelCatalog
from toolang.plugin.catalogs.ollama import OllamaModelCatalog


def test_ollama_catalog_enriches_models_from_tags_and_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        gets={
            "http://ollama.test/api/tags": {
                "models": [
                    {
                        "name": "gemma3:4b",
                        "modified_at": "2026-08-23T10:20:30Z",
                        "size": 3_338_801_804,
                        "digest": "sha256:test",
                        "details": {
                            "format": "gguf",
                            "family": "gemma",
                            "parameter_size": "4.3B",
                            "quantization_level": "Q4_K_M",
                        },
                    }
                ]
            }
        },
        posts={
            ("http://ollama.test/api/show", "gemma3:4b"): {
                "modified_at": "2026-08-24T10:20:30Z",
                "capabilities": [
                    "completion",
                    "vision",
                    "audio",
                    "tools",
                    "thinking",
                ],
                "details": {"family": "gemma3"},
                "model_info": {"gemma3.context_length": 131_072},
                "parameters": "temperature 0.7\nnum_ctx 32768\nnum_predict 8192",
            }
        },
    )
    monkeypatch.setattr(httpx, "AsyncClient", client.factory)

    snapshot = asyncio.run(
        OllamaModelCatalog({}, endpoint="http://ollama.test").snapshot()
    )
    model = snapshot.find("ollama", "gemma3:4b")

    assert model is not None
    assert model.family == "gemma3"
    assert model.last_updated == "2026-08-24"
    assert model.limit == {"context": 32_768, "output": 8192}
    assert model.modalities == {
        "input": ("text", "image", "audio"),
        "output": ("text",),
    }
    assert model.attachment is True
    assert model.reasoning is True
    assert model.tool_call is True
    assert model.temperature is True
    assert model.structured_output is True
    assert model.cost == LOCAL_ZERO_COST
    assert client.posts == [
        (
            "http://ollama.test/api/show",
            {"model": "gemma3:4b", "verbose": False},
        )
    ]


def test_llama_cpp_catalog_combines_model_meta_and_server_props(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        gets={
            "http://llama.test/v1/models": {
                "data": [
                    {
                        "id": "llama-3.1-8b",
                        "created": 1_735_142_223,
                        "owned_by": "llamacpp",
                        "meta": {
                            "n_ctx_train": 131_072,
                            "n_params": 8_030_261_312,
                            "size": 4_912_898_304,
                        },
                    }
                ]
            },
            "http://llama.test/props": {
                "default_generation_settings": {
                    "n_ctx": 65_536,
                    "params": {"n_predict": 4_096},
                },
                "model_path": "llama-3.1-8b",
                "build_info": "b123-test",
                "total_slots": 2,
                "chat_template_caps": {
                    "supports_tools": True,
                    "supports_tool_calls": True,
                    "supports_thinking": True,
                },
                "modalities": {"vision": True},
            },
        }
    )
    monkeypatch.setattr(httpx, "AsyncClient", client.factory)

    snapshot = asyncio.run(
        LlamaCppModelCatalog({}, endpoint="http://llama.test/v1").snapshot()
    )
    model = snapshot.find("llama_cpp", "llama-3.1-8b")

    assert model is not None
    assert model.limit == {"context": 65_536, "output": 4_096}
    assert model.modalities == {"input": ("text", "image"), "output": ("text",)}
    assert model.attachment is True
    assert model.reasoning is True
    assert model.tool_call is True
    assert model.temperature is True
    assert model.structured_output is True
    assert model.cost == LOCAL_ZERO_COST


def test_local_detail_failures_keep_list_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ollama = _FakeClient(
        gets={
            "http://ollama.test/api/tags": {
                "models": [
                    {
                        "name": "qwen3:8b",
                        "details": {"family": "qwen3", "parameter_size": "8B"},
                    }
                ]
            }
        },
        posts={
            ("http://ollama.test/api/show", "qwen3:8b"): httpx.ConnectError(
                "show unavailable"
            )
        },
    )
    monkeypatch.setattr(httpx, "AsyncClient", ollama.factory)

    ollama_snapshot = asyncio.run(
        OllamaModelCatalog({}, endpoint="http://ollama.test").snapshot()
    )
    ollama_model = ollama_snapshot.find("ollama", "qwen3:8b")

    assert ollama_model is not None
    assert ollama_model.family == "qwen3"
    assert ollama_model.limit == {}
    assert ollama_model.tool_call is None
    assert ollama_model.cost == LOCAL_ZERO_COST

    llama_cpp = _FakeClient(
        gets={
            "http://llama.test/v1/models": {
                "data": [{"id": "local", "meta": {"n_ctx_train": 32_768}}]
            },
            "http://llama.test/props": httpx.ConnectError("props unavailable"),
        }
    )
    monkeypatch.setattr(httpx, "AsyncClient", llama_cpp.factory)

    llama_snapshot = asyncio.run(
        LlamaCppModelCatalog({}, endpoint="http://llama.test").snapshot()
    )
    llama_model = llama_snapshot.find("llama_cpp", "local")

    assert llama_model is not None
    assert llama_model.limit == {}
    assert llama_model.tool_call is None
    assert llama_model.cost == LOCAL_ZERO_COST


def test_local_list_failure_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        gets={"http://ollama.test/api/tags": httpx.ConnectError("endpoint unavailable")}
    )
    monkeypatch.setattr(httpx, "AsyncClient", client.factory)

    snapshot = asyncio.run(
        OllamaModelCatalog({}, endpoint="http://ollama.test").snapshot()
    )

    assert snapshot.models == ()


def test_local_catalog_endpoints_use_the_docker_host_gateway() -> None:
    environ = {"TOOLANG_HOST_GATEWAY": "host.docker.internal"}

    assert ollama_models._ollama_host(None, environ) == (
        "http://host.docker.internal:11434"
    )
    assert llama_cpp_models._llama_cpp_endpoint(None, environ) == (
        "http://host.docker.internal:8080/v1"
    )
    assert (
        ollama_models._ollama_host(
            None,
            {**environ, "OLLAMA_HOST": "http://localhost:1234"},
        )
        == "http://host.docker.internal:1234"
    )
    assert (
        llama_cpp_models._llama_cpp_endpoint(
            None,
            {**environ, "LLAMA_CPP_HOST": "http://127.0.0.1:4321"},
        )
        == "http://host.docker.internal:4321/v1"
    )


def test_explicit_local_catalog_endpoints_are_not_rewritten() -> None:
    environ = {"TOOLANG_HOST_GATEWAY": "host.docker.internal"}

    assert ollama_models._ollama_host("http://127.0.0.1:11434", environ) == (
        "http://127.0.0.1:11434"
    )
    assert (
        llama_cpp_models._llama_cpp_endpoint(
            "http://localhost:8080",
            environ,
        )
        == "http://localhost:8080/v1"
    )


def test_local_probes_distinguish_unreachable_from_invalid_responses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        ollama_models.httpx,
        "AsyncClient",
        _FakeClient(
            gets={"http://ollama.test/api/tags": httpx.ConnectError("down")}
        ).factory,
    )

    with caplog.at_level(logging.DEBUG, logger="toolang.plugin.catalogs.ollama"):
        ollama = asyncio.run(
            OllamaModelCatalog({}, endpoint="http://ollama.test").snapshot()
        )

    assert ollama.models == ()
    assert "catalog.ollama.unreachable" in caplog.text

    caplog.clear()
    monkeypatch.setattr(
        llama_cpp_models.httpx,
        "AsyncClient",
        _FakeClient(
            gets={"http://llama.test/v1/models": ValueError("bad payload")}
        ).factory,
    )

    with caplog.at_level(logging.WARNING, logger="toolang.plugin.catalogs.llama_cpp"):
        llama_cpp = asyncio.run(
            LlamaCppModelCatalog({}, endpoint="http://llama.test/v1").snapshot()
        )

    assert llama_cpp.models == ()
    assert "catalog.llama_cpp.invalid_response" in caplog.text


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return

    def json(self) -> object:
        return self.payload


class _FakeClient:
    def __init__(
        self,
        *,
        gets: Mapping[str, object],
        posts: Mapping[tuple[str, str], object] | None = None,
    ) -> None:
        self.gets = dict(gets)
        self.post_payloads = dict(posts or {})
        self.posts: list[tuple[str, object]] = []

    def factory(self, *, timeout: float, headers=None) -> _FakeClient:
        assert timeout == 2.0
        return self

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return

    async def get(self, url: str) -> _FakeResponse:
        if url not in self.gets:
            response = httpx.Response(404, request=httpx.Request("GET", url))
            response.raise_for_status()
        payload = self.gets[url]
        if isinstance(payload, Exception):
            raise payload
        return _FakeResponse(payload)

    async def post(self, url: str, *, json: Mapping[str, object]) -> _FakeResponse:
        payload = dict(json)
        self.posts.append((url, payload))
        response = self.post_payloads[(url, str(payload["model"]))]
        if isinstance(response, Exception):
            raise response
        return _FakeResponse(response)


@pytest.mark.parametrize(
    "parameters,loaded,expected",
    [
        ("", None, {}),
        ('num_ctx "8192"\nnum_predict "2048"', None, {"context": 8192, "output": 2048}),
        ("num_ctx 8192\nnum_predict 2048", 4096, {"context": 4096, "output": 2048}),
        (
            "num_ctx 8192\nnum_ctx 16384\nnum_predict 1024\nnum_predict -1",
            None,
            {"context": 16384},
        ),
        ("num_ctx true\nnum_predict invalid", None, {}),
        ('num_ctx 8192\nnum_ctx "bad', None, {}),
        ("num_ctx -1\nnum_predict -2", None, {}),
    ],
)
def test_ollama_limits_describe_configured_route(
    parameters, loaded, expected, monkeypatch
):
    entry = {"name": "same-model", "capabilities": ["completion", "tools"]}
    client = _FakeClient(
        gets={
            "http://service.test/api/tags": {"models": [entry]},
            "http://service.test/api/ps": {
                "models": [{"model": "same-model", "context_length": loaded}]
            },
        },
        posts={
            ("http://service.test/api/show", "same-model"): {
                "parameters": parameters,
                "model_info": {"model.context_length": 1000000},
            }
        },
    )
    monkeypatch.setattr(httpx, "AsyncClient", client.factory)
    snapshot = asyncio.run(
        OllamaModelCatalog({}, endpoint="http://service.test").snapshot()
    )
    model = snapshot.models[0]
    assert model.limit == expected
    assert model.tool_call is True
    assert not hasattr(model, "_toolang")
    assert "defaults" not in model.to_data()


@pytest.mark.parametrize("primary", [-1, 0, True, "invalid"])
def test_llama_prediction_primary_is_not_replaced_by_positive_alias(primary):
    model = llama_cpp_models._llama_cpp_model(
        "m",
        {"meta": {"n_ctx": 4096, "n_ctx_train": 131072}},
        {
            "default_generation_settings": {
                "params": {"n_predict": primary, "max_tokens": 8192}
            },
            "chat_template_caps": {
                "supports_reasoning_effort": False,
                "supports_tools": True,
            },
            "modalities": {"audio": True},
        },
    )
    assert model.limit == {"context": 4096}
    assert model.reasoning is None
    assert model.reasoning_options is None
    assert model.tool_call is None
    assert model.attachment is True


def test_llama_router_uses_model_specific_props_without_autoload(monkeypatch):
    client = _FakeClient(
        gets={
            "http://service.test/v1/models": {
                "data": [{"id": "a/model"}, {"id": "b/model"}]
            },
            "http://service.test/props?model=a%2Fmodel&autoload=false": {
                "default_generation_settings": {
                    "n_ctx": 32768,
                    "params": {"n_predict": 8192},
                },
            },
            "http://service.test/props?model=b%2Fmodel&autoload=false": {
                "default_generation_settings": {
                    "n_ctx": 131072,
                    "params": {"n_predict": 16384},
                },
            },
        }
    )
    monkeypatch.setattr(httpx, "AsyncClient", client.factory)
    snapshot = asyncio.run(
        LlamaCppModelCatalog({}, endpoint="http://service.test").snapshot()
    )
    assert [dict(model.limit) for model in snapshot.models] == [
        {"context": 32768, "output": 8192},
        {"context": 131072, "output": 16384},
    ]


@pytest.mark.parametrize(
    "factory",
    [ollama_models.create_model_catalog, llama_cpp_models.create_model_catalog],
)
@pytest.mark.parametrize(
    "config",
    [
        {"endpoint": 123},
        {"endpoint": ""},
        {"timeout": 0},
        {"timeout": float("inf")},
        {"headers": {"Authorization": 1}},
    ],
)
def test_invalid_discovery_configuration_fails_clearly(factory, config):
    with pytest.raises((ValueError, TypeError)):
        factory(config)


def test_discovery_credentials_are_not_published(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={"models": [{"model": "m"}]}
            if request.url.path == "/api/tags"
            else {},
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    catalog = ollama_models.create_model_catalog(
        {
            "endpoint": "http://service.test",
            "headers": {"Authorization": "Bearer private"},
        }
    )
    snapshot = asyncio.run(catalog.snapshot())
    assert all(request.headers["authorization"] == "Bearer private" for request in seen)
    assert "private" not in repr(snapshot)


def test_same_checkpoint_reports_different_server_limits(monkeypatch):
    settings = {"a.test": (32768, 8192), "b.test": (131072, 16384)}

    def handler(request):
        context, output = settings[request.url.host]
        if request.url.path == "/api/tags":
            payload = {"models": [{"model": "same:latest"}]}
        elif request.url.path == "/api/show":
            payload = {"parameters": f"num_ctx {context}\nnum_predict {output}"}
        else:
            payload = {"models": []}
        return httpx.Response(200, json=payload)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    snapshots = [
        asyncio.run(OllamaModelCatalog({}, endpoint=f"http://{host}").snapshot())
        for host in settings
    ]
    assert snapshots[0].models[0].id == snapshots[1].models[0].id
    assert [dict(snapshot.models[0].limit) for snapshot in snapshots] == [
        {"context": context, "output": output} for context, output in settings.values()
    ]
    assert [snapshot.providers["ollama"].api for snapshot in snapshots] == [
        "http://a.test/v1",
        "http://b.test/v1",
    ]
