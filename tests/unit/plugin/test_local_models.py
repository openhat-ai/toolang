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
                "parameters": "temperature 0.7",
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
    assert model.limit == {"context": 131_072}
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
    assert llama_model.limit == {"context": 32_768}
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

    def factory(self, *, timeout: float) -> _FakeClient:
        assert timeout == 2.0
        return self

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return

    async def get(self, url: str) -> _FakeResponse:
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
