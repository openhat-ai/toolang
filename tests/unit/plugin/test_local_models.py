from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import cast

import httpx
import pytest

from toolang.plugin.models import local as local_models
from toolang.plugin.models.local import LlamaCppModels, OllamaModels


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
    monkeypatch.setattr(local_models.httpx, "AsyncClient", client.factory)

    snapshot = asyncio.run(OllamaModels({}, endpoint="http://ollama.test").snapshot())
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
    assert model.cost == {"input": 0, "output": 0}
    runtime = cast(Mapping[str, object], model.extra["runtime"])
    assert runtime["size"] == 3_338_801_804
    details = cast(Mapping[str, object], runtime["details"])
    assert details["quantization_level"] == "Q4_K_M"
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
    monkeypatch.setattr(local_models.httpx, "AsyncClient", client.factory)

    snapshot = asyncio.run(
        LlamaCppModels({}, endpoint="http://llama.test/v1").snapshot()
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
    assert model.cost == {"input": 0, "output": 0}
    runtime = cast(Mapping[str, object], model.extra["runtime"])
    meta = cast(Mapping[str, object], runtime["meta"])
    assert meta["n_params"] == 8_030_261_312
    assert runtime["build_info"] == "b123-test"


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
    monkeypatch.setattr(local_models.httpx, "AsyncClient", ollama.factory)

    ollama_snapshot = asyncio.run(
        OllamaModels({}, endpoint="http://ollama.test").snapshot()
    )
    ollama_model = ollama_snapshot.find("ollama", "qwen3:8b")

    assert ollama_model is not None
    assert ollama_model.family == "qwen3"
    assert ollama_model.limit == {}
    assert ollama_model.tool_call is None
    assert ollama_model.cost == {"input": 0, "output": 0}

    llama_cpp = _FakeClient(
        gets={
            "http://llama.test/v1/models": {
                "data": [{"id": "local", "meta": {"n_ctx_train": 32_768}}]
            },
            "http://llama.test/props": httpx.ConnectError("props unavailable"),
        }
    )
    monkeypatch.setattr(local_models.httpx, "AsyncClient", llama_cpp.factory)

    llama_snapshot = asyncio.run(
        LlamaCppModels({}, endpoint="http://llama.test").snapshot()
    )
    llama_model = llama_snapshot.find("llama_cpp", "local")

    assert llama_model is not None
    assert llama_model.limit == {"context": 32_768}
    assert llama_model.tool_call is None
    assert llama_model.cost == {"input": 0, "output": 0}


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
