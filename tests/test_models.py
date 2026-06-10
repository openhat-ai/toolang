from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.base.protocols.model import ModelProvider
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import (
    AudioPart,
    FilePart,
    ImagePart,
    Message,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.base.types.run import ModelCall, ModelCallResult, ModelUsage, ToolCall
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.base.error import ToolangError
from toolang.execution.context import RunContext, RunSnapshot, SnapshotAgent, SnapshotProgram, SnapshotRun
from toolang.execution.input import RunBinding, RunInput
from toolang.models.resolution import resolve_model, select_model_selectors
from toolang.models.views import model_target_profile
from toolang.models.providers import deepseek as deepseek_models
from toolang.models.providers import google as google_models
from toolang.models.providers import ollama as ollama_models
from toolang.models.providers import openai as openai_models
from toolang.models.providers import openrouter as openrouter_models
from toolang.up import load_model_adapters
from toolang.up import load_model_providers
from toolang.models.adapters import chat_completions as chat_completions_models
from toolang.models.adapters import responses as responses_models
from toolang.models.adapters.responses import encode_message, response_payload
from toolang.lang.ast import MessageBlock, ParamDecl, SourceSpan, Thunk
from toolang.plugin import load_loop, load_loops
from toolang.models.config import load_default_models, load_model_aliases


class _FakeTool(AgentTool):
    name = "shell__execute"
    plugin_name = "shell"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Run a shell command.",
            parameters={"type": "object"},
        )

    def invoke(self, arguments, context: ToolContext) -> dict[str, Any]:
        del context
        return {"ok": True, "stdout": f"ran:{arguments['command']}"}


class _FakeModelProvider(ModelProvider):
    def __init__(
        self,
        *,
        name: str,
        models: tuple[ModelInfo, ...] = (),
        responses: list[ModelCallResult] | None = None,
        required_env_vars: tuple[str, ...] = (),
        default_base_url: str | None = None,
        default_api_key_env: str | None = None,
    ) -> None:
        self.name = name
        self.description = None
        self._models = tuple(models)
        self._responses = list(responses or [])
        self._required_env_vars = tuple(required_env_vars)
        self._default_base_url = default_base_url
        self._default_api_key_env = default_api_key_env
        self.requests: list[ModelCall] = []
        self.list_models_calls = 0

    def required_env_vars(self) -> tuple[str, ...]:
        return self._required_env_vars

    def default_base_url(self, *, environ) -> str | None:
        del environ
        return self._default_base_url

    def default_api_key_env(self) -> str | None:
        return self._default_api_key_env

    def list_models(self, *, environ) -> tuple[ModelInfo, ...]:
        del environ
        self.list_models_calls += 1
        return self._models

    def invoke(self, target: ModelTarget, request: ModelCall) -> ModelCallResult:
        del target
        self.requests.append(request)
        return self._responses.pop(0)

    def stream(self, target: ModelTarget, request: ModelCall, *, on_event) -> ModelCallResult:
        del on_event
        return self.invoke(target, request)


def test_model_resolution_resolves_named_route(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    (toolang_root / "agents" / "alice").mkdir(parents=True, exist_ok=True)
    (toolang_root / "config.toml").write_text(
        '[models]\n'
        'default = ["fast"]\n'
        '\n'
        '[models.aliases.fast]\n'
        'ref = "openai/gpt-5"\n'
        'provider = "openai"\n',
        encoding="utf-8",
    )
    provider = _FakeModelProvider(
        name="openai",
        models=(
            ModelInfo(
                ref="openai/gpt-5",
                provider="openai",
                name="gpt-5",
                model="gpt-5",
                selectors=("gpt-5", "openai/gpt-5"),
                adapter="responses",
            ),
        ),
        default_api_key_env="OPENAI_API_KEY",
    )
    context = SimpleNamespace(
        model_providers={"openai": provider},
        model_aliases=load_model_aliases(toolang_root, "alice"),
        default_models=load_default_models(toolang_root, "alice"),
        model_environ={"OPENAI_API_KEY": "secret"},
    )

    target = resolve_model(context, selector="fast")

    assert target.ref == "openai/gpt-5"
    assert target.provider == "openai"
    assert target.model == "gpt-5"
    assert target.api_key == "secret"


def test_model_resolution_resolves_explicit_provider_route() -> None:
    provider = _FakeModelProvider(
        name="openrouter",
        models=(
            ModelInfo(
                ref="openai/gpt-5",
                provider="openrouter",
                name="gpt-5",
                model="openai/gpt-5",
                selectors=("gpt-5", "openai/gpt-5"),
                adapter="responses",
            ),
        ),
    )
    context = SimpleNamespace(
        model_providers={"openrouter": provider},
        model_aliases={},
        default_models=(),
        model_environ={},
    )

    target = resolve_model(context, selector="openai/gpt-5[openrouter]")

    assert target.provider == "openrouter"
    assert target.model == "openai/gpt-5"


def test_model_resolution_rejects_ambiguous_selector() -> None:
    context = SimpleNamespace(
        model_providers={
            "openai": _FakeModelProvider(
                name="openai",
                models=(
                    ModelInfo(
                        ref="openai/gpt-5",
                        provider="openai",
                        name="gpt-5",
                        model="gpt-5",
                        selectors=("gpt-5", "openai/gpt-5"),
                        adapter="responses",
                    ),
                ),
            ),
            "openrouter": _FakeModelProvider(
                name="openrouter",
                models=(
                    ModelInfo(
                        ref="openai/gpt-5",
                        provider="openrouter",
                        name="gpt-5",
                        model="openai/gpt-5",
                        selectors=("gpt-5", "openai/gpt-5"),
                        adapter="responses",
                    ),
                ),
            ),
        },
        model_aliases={},
        default_models=(),
        model_environ={},
    )

    with pytest.raises(ToolangError, match="ambiguous"):
        resolve_model(context, selector="openai/gpt-5")


def test_model_resolution_rejects_missing_provider_env_before_target_use() -> None:
    provider = _FakeModelProvider(
        name="openai",
        models=(
            ModelInfo(
                ref="openai/gpt-5",
                provider="openai",
                name="gpt-5",
                model="gpt-5",
                selectors=("gpt-5", "openai/gpt-5"),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENAI_API_KEY",),
    )
    context = SimpleNamespace(
        model_providers={"openai": provider},
        model_aliases={},
        default_models=(),
        model_environ={},
    )

    with pytest.raises(ToolangError, match="OPENAI_API_KEY"):
        resolve_model(context, selector="openai/gpt-5[openai]")


def test_model_resolution_skips_unconfigured_provider_when_configured_match_exists() -> None:
    openai = _FakeModelProvider(
        name="openai",
        models=(
            ModelInfo(
                ref="openai/gpt-5",
                provider="openai",
                name="gpt-5",
                model="gpt-5",
                selectors=("gpt-5", "openai/gpt-5"),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENAI_API_KEY",),
    )
    openrouter = _FakeModelProvider(
        name="openrouter",
        models=(
            ModelInfo(
                ref="openai/gpt-5",
                provider="openrouter",
                name="gpt-5",
                model="openai/gpt-5",
                selectors=("gpt-5", "openai/gpt-5"),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENROUTER_API_KEY",),
    )
    context = SimpleNamespace(
        model_providers={"openai": openai, "openrouter": openrouter},
        model_aliases={},
        default_models=(),
        model_environ={"OPENROUTER_API_KEY": "secret"},
    )

    target = resolve_model(context, selector="gpt-5")

    assert target.provider == "openrouter"


def test_model_resolution_uses_first_allowed_selector_as_default() -> None:
    provider = _FakeModelProvider(
        name="openrouter",
        models=(
            ModelInfo(
                ref="openai/gpt-5",
                provider="openrouter",
                name="gpt-5",
                model="gpt-5",
                selectors=("gpt-5", "openai/gpt-5"),
                adapter="responses",
            ),
            ModelInfo(
                ref="openai/o3",
                provider="openrouter",
                name="o3",
                model="o3",
                selectors=("o3", "openai/o3"),
                adapter="responses",
            ),
        ),
    )
    context = SimpleNamespace(
        model_providers={"openrouter": provider},
        model_aliases={},
        default_models=(),
        model_environ={},
    )

    target = resolve_model(
        context,
        selector=None,
        default_selector="gpt-5[openrouter]",
        allowed_selectors=("gpt-5[openrouter]", "o3[openrouter]"),
    )

    assert target.ref == "openai/gpt-5"
    assert target.model == "gpt-5"


def test_model_resolution_allows_selector_within_allowed_set() -> None:
    provider = _FakeModelProvider(
        name="openrouter",
        models=(
            ModelInfo(ref="openai/gpt-5", provider="openrouter", name="gpt-5", model="gpt-5", selectors=("gpt-5",), adapter="responses"),
            ModelInfo(ref="openai/o3", provider="openrouter", name="o3", model="o3", selectors=("o3",), adapter="responses"),
        ),
    )
    context = SimpleNamespace(
        model_providers={"openrouter": provider},
        model_aliases={},
        default_models=(),
        model_environ={},
    )

    target = resolve_model(
        context,
        selector="o3[openrouter]",
        default_selector="gpt-5[openrouter]",
        allowed_selectors=("gpt-5[openrouter]", "o3[openrouter]"),
    )

    assert target.ref == "openai/o3"
    assert target.model == "o3"


def test_model_resolution_rejects_selector_outside_allowed_set() -> None:
    provider = _FakeModelProvider(
        name="openrouter",
        models=(
            ModelInfo(ref="openai/gpt-5", provider="openrouter", name="gpt-5", model="gpt-5", selectors=("gpt-5",), adapter="responses"),
            ModelInfo(ref="openai/o3", provider="openrouter", name="o3", model="o3", selectors=("o3",), adapter="responses"),
        ),
    )
    context = SimpleNamespace(
        model_providers={"openrouter": provider},
        model_aliases={},
        default_models=(),
        model_environ={},
    )

    with pytest.raises(ToolangError, match="not allowed for this activation"):
        resolve_model(
            context,
            selector="o3[openrouter]",
            default_selector="gpt-5[openrouter]",
            allowed_selectors=("gpt-5[openrouter]",),
        )


def test_model_resolution_reports_no_matched_models_when_selector_misses() -> None:
    provider = _FakeModelProvider(
        name="openrouter",
        models=(
            ModelInfo(
                ref="openai/gpt-5",
                provider="openrouter",
                name="gpt-5",
                model="openai/gpt-5",
                selectors=("gpt-5", "openai/gpt-5"),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENROUTER_API_KEY",),
    )
    context = SimpleNamespace(
        model_providers={"openrouter": provider},
        model_aliases={},
        default_models=(),
        model_environ={"OPENROUTER_API_KEY": "secret"},
    )

    with pytest.raises(ToolangError, match="No matched models."):
        resolve_model(context, selector="anthropic/claude-sonnet-4.5")


def test_select_model_selectors_preserves_activation_order_for_intersection() -> None:
    provider = _FakeModelProvider(
        name="openrouter",
        models=(
            ModelInfo(ref="openai/gpt-5", provider="openrouter", name="gpt-5", model="gpt-5", selectors=("gpt-5", "openai/gpt-5"), adapter="responses"),
            ModelInfo(ref="openai/o3", provider="openrouter", name="o3", model="o3", selectors=("o3", "openai/o3"), adapter="responses"),
        ),
    )
    context = SimpleNamespace(
        model_providers={"openrouter": provider},
        model_aliases={},
        default_models=(),
        model_environ={},
    )

    selectors = select_model_selectors(
        context,
        thunk_selectors=("openai/gpt-5", "openai/o3"),
        activation_selectors=("openai/o3[openrouter]", "openai/gpt-5[openrouter]"),
    )

    assert selectors == ("openai/o3[openrouter]", "openai/gpt-5[openrouter]")


def test_select_model_selectors_supports_name_glob_without_matching_family() -> None:
    provider = _FakeModelProvider(
        name="openrouter",
        models=(
            ModelInfo(ref="openai/gpt-5", provider="openrouter", name="gpt-5", model="gpt-5", selectors=("gpt-5", "openai/gpt-5"), adapter="responses"),
            ModelInfo(ref="anthropic/claude-sonnet", provider="openrouter", name="claude-sonnet", model="claude-sonnet", selectors=("claude-sonnet", "anthropic/claude-sonnet"), adapter="responses"),
        ),
    )
    context = SimpleNamespace(
        model_providers={"openrouter": provider},
        model_aliases={},
        default_models=(),
        model_environ={},
    )

    assert select_model_selectors(context, activation_selectors=("gpt-*",)) == (
        "openai/gpt-5[openrouter]",
    )
    assert select_model_selectors(context, activation_selectors=("openai/*",)) == (
        "openai/gpt-5[openrouter]",
    )
    with pytest.raises(ToolangError, match="No matched models."):
        select_model_selectors(context, activation_selectors=("openai",))


def test_select_model_selectors_expands_route_neutral_thunk_refs_from_discovery() -> None:
    openai = _FakeModelProvider(
        name="openai",
        models=(
            ModelInfo(ref="openai/gpt-5", provider="openai", name="gpt-5", model="gpt-5", selectors=("gpt-5", "openai/gpt-5"), adapter="responses"),
            ModelInfo(ref="openai/o3", provider="openai", name="o3", model="o3", selectors=("o3", "openai/o3"), adapter="responses"),
        ),
        required_env_vars=("OPENAI_API_KEY",),
    )
    openrouter = _FakeModelProvider(
        name="openrouter",
        models=(
            ModelInfo(ref="openai/gpt-5", provider="openrouter", name="gpt-5", model="openai/gpt-5", selectors=("gpt-5", "openai/gpt-5"), adapter="responses"),
            ModelInfo(ref="openai/o3", provider="openrouter", name="o3", model="openai/o3", selectors=("o3", "openai/o3"), adapter="responses"),
        ),
        required_env_vars=("OPENROUTER_API_KEY",),
    )
    context = SimpleNamespace(
        model_providers={"openai": openai, "openrouter": openrouter},
        model_aliases={},
        default_models=(),
        model_environ={"OPENAI_API_KEY": "secret", "OPENROUTER_API_KEY": "secret"},
    )

    selectors = select_model_selectors(
        context,
        thunk_selectors=("openai/o3", "openai/gpt-5"),
    )

    assert selectors == (
        "openai/o3[openai]",
        "openai/o3[openrouter]",
        "openai/gpt-5[openai]",
        "openai/gpt-5[openrouter]",
    )


def test_select_model_selectors_skips_providers_missing_required_env() -> None:
    openai = _FakeModelProvider(
        name="openai",
        models=(
            ModelInfo(ref="openai/gpt-5", provider="openai", name="gpt-5", model="gpt-5", selectors=("gpt-5", "openai/gpt-5"), adapter="responses"),
        ),
        required_env_vars=("OPENAI_API_KEY",),
    )
    openrouter = _FakeModelProvider(
        name="openrouter",
        models=(
            ModelInfo(ref="openai/gpt-5", provider="openrouter", name="gpt-5", model="openai/gpt-5", selectors=("gpt-5", "openai/gpt-5"), adapter="responses"),
        ),
        required_env_vars=("OPENROUTER_API_KEY",),
    )
    context = SimpleNamespace(
        model_providers={"openai": openai, "openrouter": openrouter},
        model_aliases={},
        default_models=(),
        model_environ={"OPENROUTER_API_KEY": "secret"},
    )

    selectors = select_model_selectors(
        context,
        thunk_selectors=("openai/gpt-5",),
    )

    assert selectors == ("openai/gpt-5[openrouter]",)


def test_select_model_selectors_prefers_exact_ref_over_version_aliases() -> None:
    openrouter = _FakeModelProvider(
        name="openrouter",
        models=(
            ModelInfo(
                ref="openai/gpt-5",
                provider="openrouter",
                name="gpt-5",
                model="openai/gpt-5",
                selectors=("gpt-5", "openai/gpt-5"),
                adapter="responses",
            ),
            ModelInfo(
                ref="openai/gpt-5-2025-08-07",
                provider="openrouter",
                name="gpt-5-2025-08-07",
                model="openai/gpt-5",
                selectors=("gpt-5-2025-08-07", "openai/gpt-5-2025-08-07", "openai/gpt-5"),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENROUTER_API_KEY",),
    )
    context = SimpleNamespace(
        model_providers={"openrouter": openrouter},
        model_aliases={},
        default_models=(),
        model_environ={"OPENROUTER_API_KEY": "secret"},
    )

    selectors = select_model_selectors(
        context,
        thunk_selectors=("openai/gpt-5",),
    )

    assert selectors == ("openai/gpt-5[openrouter]",)


def test_select_model_selectors_returns_all_discoverable_when_unrestricted() -> None:
    openai = _FakeModelProvider(
        name="openai",
        models=(
            ModelInfo(ref="openai/gpt-5", provider="openai", name="gpt-5", model="gpt-5", selectors=("gpt-5", "openai/gpt-5"), adapter="responses"),
            ModelInfo(ref="openai/o3", provider="openai", name="o3", model="o3", selectors=("o3", "openai/o3"), adapter="responses"),
        ),
        required_env_vars=("OPENAI_API_KEY",),
    )
    openrouter = _FakeModelProvider(
        name="openrouter",
        models=(
            ModelInfo(ref="openai/gpt-5", provider="openrouter", name="gpt-5", model="openai/gpt-5", selectors=("gpt-5", "openai/gpt-5"), adapter="responses"),
            ModelInfo(ref="openai/o3", provider="openrouter", name="o3", model="openai/o3", selectors=("o3", "openai/o3"), adapter="responses"),
        ),
        required_env_vars=("OPENROUTER_API_KEY",),
    )
    context = SimpleNamespace(
        model_providers={"openai": openai, "openrouter": openrouter},
        model_aliases={},
        default_models=(),
        model_environ={"OPENAI_API_KEY": "secret", "OPENROUTER_API_KEY": "secret"},
    )

    selectors = select_model_selectors(context)

    assert selectors == (
        "openai/gpt-5[openai]",
        "openai/gpt-5[openrouter]",
        "openai/o3[openai]",
        "openai/o3[openrouter]",
    )


def test_model_info_discovery_is_cached_within_one_process() -> None:
    openrouter = _FakeModelProvider(
        name="openrouter",
        models=(
            ModelInfo(
                ref="anthropic/claude-4.5-sonnet-20250929",
                provider="openrouter",
                name="claude-4.5-sonnet-20250929",
                model="anthropic/claude-sonnet-4.5",
                selectors=("anthropic/claude-sonnet-4.5", "anthropic/claude-4.5-sonnet-20250929"),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENROUTER_API_KEY",),
    )
    context = SimpleNamespace(
        model_providers={"openrouter": openrouter},
        model_aliases={},
        default_models=(),
        model_environ={"OPENROUTER_API_KEY": "secret"},
    )

    selectors = select_model_selectors(
        context,
        thunk_selectors=("anthropic/claude-4.5-sonnet-20250929",),
    )
    target = resolve_model(context, selector=selectors[0])

    assert selectors == ("anthropic/claude-4.5-sonnet-20250929[openrouter]",)
    assert target.ref == "anthropic/claude-4.5-sonnet-20250929"
    assert openrouter.list_models_calls == 1


def test_model_route_can_override_provider_defaults(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    (toolang_root / "agents" / "alice").mkdir(parents=True, exist_ok=True)
    (toolang_root / "config.toml").write_text(
        '[models.aliases.gateway]\n'
        'ref = "openai/gpt-5"\n'
        'provider = "openai"\n'
        'adapter = "responses"\n'
        'endpoint = "https://gateway.example.com/v1"\n'
        'key_env = "GATEWAY_API_KEY"\n'
        'headers = { "X-Team" = "infra" }\n',
        encoding="utf-8",
    )
    provider = _FakeModelProvider(
        name="openai",
        models=(),
        default_base_url="https://api.openai.com/v1",
        default_api_key_env="OPENAI_API_KEY",
    )
    context = SimpleNamespace(
        model_providers={"openai": provider},
        model_aliases=load_model_aliases(toolang_root, "alice"),
        default_models=(),
        model_environ={"GATEWAY_API_KEY": "secret"},
    )

    target = resolve_model(context, selector="gateway")

    assert target.ref == "openai/gpt-5"
    assert target.provider == "openai"
    assert target.model == "gpt-5"
    assert target.adapter == "responses"
    assert target.base_url == "https://gateway.example.com/v1"
    assert target.api_key == "secret"
    assert target.headers == {"X-Team": "infra"}


def test_model_alias_uses_provider_default_key_env(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    (toolang_root / "agents" / "alice").mkdir(parents=True, exist_ok=True)
    (toolang_root / "config.toml").write_text(
        '[models.aliases.qwen]\n'
        'ref = "qwen/qwen3-coder"\n'
        'provider = "openrouter"\n',
        encoding="utf-8",
    )
    provider = _FakeModelProvider(
        name="openrouter",
        models=(),
        default_base_url="https://openrouter.ai/api/v1",
        default_api_key_env="OPENROUTER_API_KEY",
    )
    context = SimpleNamespace(
        model_providers={"openrouter": provider},
        model_aliases=load_model_aliases(toolang_root, "alice"),
        default_models=(),
        model_environ={"OPENROUTER_API_KEY": "secret"},
    )

    target = resolve_model(context, selector="qwen")

    assert target.ref == "qwen/qwen3-coder"
    assert target.provider == "openrouter"
    assert target.model == "qwen/qwen3-coder"
    assert target.adapter == "chat_completions"
    assert target.base_url == "https://openrouter.ai/api/v1"
    assert target.api_key == "secret"


def test_model_alias_reports_missing_key_env(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    (toolang_root / "agents" / "alice").mkdir(parents=True, exist_ok=True)
    (toolang_root / "config.toml").write_text(
        '[models.aliases.gateway]\n'
        'ref = "openai/gpt-5"\n'
        'provider = "openai"\n'
        'adapter = "responses"\n'
        'key_env = "GATEWAY_API_KEY"\n',
        encoding="utf-8",
    )
    provider = _FakeModelProvider(
        name="openai",
        models=(),
        required_env_vars=("OPENAI_API_KEY",),
        default_api_key_env="OPENAI_API_KEY",
    )
    context = SimpleNamespace(
        model_providers={"openai": provider},
        model_aliases=load_model_aliases(toolang_root, "alice"),
        default_models=(),
        model_environ={},
    )

    with pytest.raises(ToolangError, match="model alias 'gateway'.*GATEWAY_API_KEY"):
        resolve_model(context, selector="gateway")


def test_ollama_provider_discovers_local_models(monkeypatch) -> None:
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "models": [
                    {"model": "qwen3"},
                    {"name": "llama3.2"},
                    {"model": "qwen3"},
                ]
            }

    monkeypatch.setattr(ollama_models.httpx, "get", lambda url, timeout: _Response())
    provider = ollama_models.create_model_provider({})

    models = provider.list_models(environ={})

    assert models == (
        ModelInfo(
            ref="meta/llama3.2",
            provider="ollama",
            name="llama3.2",
            model="llama3.2",
            selectors=("llama3.2", "meta/llama3.2"),
            adapter="responses",
            tools=False,
            streaming=True,
            details="Local Ollama model.",
        ),
        ModelInfo(
            ref="qwen/qwen3",
            provider="ollama",
            name="qwen3",
            model="qwen3",
            selectors=("qwen3", "qwen/qwen3"),
            adapter="responses",
            tools=True,
            streaming=True,
            details="Local Ollama model.",
        ),
    )


def test_builtin_model_provider_loader_includes_deepseek_google_and_openrouter() -> None:
    providers = load_model_providers()

    assert {"custom", "deepseek", "google", "ollama", "openai", "openrouter"} <= set(providers)
    assert providers["deepseek"].default_api_key_env() == "DEEPSEEK_API_KEY"
    assert providers["google"].default_api_key_env() == "GEMINI_API_KEY"
    assert providers["openrouter"].default_api_key_env() == "OPENROUTER_API_KEY"


def test_builtin_model_adapter_loader_includes_chat_completions_and_responses() -> None:
    adapters = load_model_adapters()

    assert "chat_completions" in adapters
    assert adapters["chat_completions"].name == "chat_completions"
    assert "responses" in adapters
    assert adapters["responses"].name == "responses"


def test_builtin_loop_loader_includes_basic() -> None:
    loops = load_loops()

    assert "basic" in loops
    assert load_loop("basic").name == "basic"


def test_openai_provider_lists_curated_common_model_profiles() -> None:
    provider = openai_models.create_model_provider({})

    by_ref = {model.ref: model for model in provider.list_models(environ={})}

    assert by_ref["openai/gpt-5.5"] == ModelInfo(
        ref="openai/gpt-5.5",
        provider="openai",
        name="gpt-5.5",
        model="gpt-5.5",
        selectors=("gpt-5.5", "openai/gpt-5.5"),
        adapter="responses",
        tools=True,
        streaming=True,
        context_window=1_050_000,
        max_output_tokens=128_000,
        input_price=0.000005,
        output_price=0.00003,
        details="Built-in OpenAI model.",
    )
    assert by_ref["openai/gpt-5.4-mini"].context_window == 400_000
    assert by_ref["openai/gpt-5.4-mini"].input_price == 0.00000075
    assert by_ref["openai/gpt-5.3-codex"].max_output_tokens == 128_000
    assert by_ref["openai/gpt-5.2-chat-latest"].max_output_tokens == 16_384
    assert by_ref["openai/gpt-5-codex"].context_window == 400_000
    assert by_ref["openai/gpt-4.1"].context_window == 1_047_576
    assert by_ref["openai/gpt-4o-mini"].max_output_tokens == 16_384


def test_deepseek_provider_lists_curated_model_profiles() -> None:
    provider = deepseek_models.create_model_provider({})

    by_ref = {model.ref: model for model in provider.list_models(environ={})}

    assert by_ref["deepseek/deepseek-v4-flash"] == ModelInfo(
        ref="deepseek/deepseek-v4-flash",
        provider="deepseek",
        name="deepseek-v4-flash",
        model="deepseek-v4-flash",
        selectors=("deepseek-v4-flash", "deepseek/deepseek-v4-flash"),
        adapter="chat_completions",
        tools=True,
        streaming=True,
        context_window=1_000_000,
        max_output_tokens=384_000,
        input_price=0.00000014,
        output_price=0.00000028,
        details="Built-in DeepSeek V4 Flash model.",
    )
    assert by_ref["deepseek/deepseek-v4-pro"].adapter == "chat_completions"
    assert by_ref["deepseek/deepseek-v4-pro"].input_price == 0.000000435
    assert by_ref["deepseek/deepseek-chat"].details == (
        "Deprecated DeepSeek compatibility model for non-thinking V4 Flash."
    )
    assert by_ref["deepseek/deepseek-reasoner"].tools is False


def test_deepseek_provider_resolves_chat_completions_target() -> None:
    provider = deepseek_models.create_model_provider({})
    context = SimpleNamespace(
        model_providers={"deepseek": provider},
        model_aliases={},
        default_models=(),
        model_environ={"DEEPSEEK_API_KEY": "secret"},
    )

    target = resolve_model(context, selector="deepseek-v4-pro")

    assert target == ModelTarget(
        ref="deepseek/deepseek-v4-pro",
        provider="deepseek",
        name="deepseek-v4-pro",
        model="deepseek-v4-pro",
        adapter="chat_completions",
        base_url="https://api.deepseek.com",
        api_key="secret",
        scope="remote",
        tools=True,
        streaming=True,
    )


def test_google_provider_lists_curated_gemini_model_profiles() -> None:
    provider = google_models.create_model_provider({})

    by_ref = {model.ref: model for model in provider.list_models(environ={})}

    assert by_ref["google/gemini-3.5-flash"] == ModelInfo(
        ref="google/gemini-3.5-flash",
        provider="google",
        name="gemini-3.5-flash",
        model="gemini-3.5-flash",
        selectors=("gemini-3.5-flash", "google/gemini-3.5-flash"),
        adapter="chat_completions",
        tools=True,
        streaming=True,
        context_window=1_048_576,
        max_output_tokens=65_536,
        details="Built-in Google Gemini 3.5 Flash model.",
    )
    assert by_ref["google/gemini-3.1-pro-preview"].tools is True
    assert by_ref["google/gemini-3-flash-preview"].streaming is True
    assert by_ref["google/gemini-2.5-flash-lite"].adapter == "chat_completions"


def test_google_provider_resolves_chat_completions_target() -> None:
    provider = google_models.create_model_provider({})
    context = SimpleNamespace(
        model_providers={"google": provider},
        model_aliases={},
        default_models=(),
        model_environ={"GEMINI_API_KEY": "secret"},
    )

    target = resolve_model(context, selector="gemini-3.5-flash")

    assert target == ModelTarget(
        ref="google/gemini-3.5-flash",
        provider="google",
        name="gemini-3.5-flash",
        model="gemini-3.5-flash",
        adapter="chat_completions",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key="secret",
        scope="remote",
        tools=True,
        streaming=True,
    )


def test_model_target_profile_formats_token_counts_as_decimal_units() -> None:
    provider = deepseek_models.create_model_provider({})
    target = ModelTarget(
        ref="deepseek/deepseek-v4-flash",
        provider="deepseek",
        name="deepseek-v4-flash",
        model="deepseek-v4-flash",
        adapter="chat_completions",
    )

    assert model_target_profile(target, provider=provider, environ={}) == (
        "streaming=y, tools=y, ctx=1M, max_out=384k, price=$0.14/$0.28"
    )
    assert model_target_profile(
        ModelTarget(
            ref="openai/gpt-5.3-chat-latest",
            provider="openai",
            name="gpt-5.3-chat-latest",
            model="gpt-5.3-chat-latest",
            adapter="responses",
        ),
        provider=openai_models.create_model_provider({}),
        environ={},
    ) == "streaming=y, tools=y, ctx=128k, max_out=16.4k, price=$1.75/$14"


def test_openrouter_provider_discovers_remote_models(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "data": [
                    {
                        "id": "openai/gpt-5",
                        "canonical_slug": "openai/gpt-5",
                        "name": "GPT-5",
                        "description": "General-purpose flagship model.",
                        "context_length": 400000,
                        "top_provider": {"max_completion_tokens": 128000},
                        "supported_parameters": ["tools", "tool_choice", "temperature"],
                        "pricing": {"prompt": "0.00000125", "completion": "0.00001"},
                    },
                    {
                        "id": "anthropic/claude-sonnet-4",
                        "canonical_slug": "anthropic/claude-sonnet-4",
                        "name": "Claude Sonnet 4",
                        "supported_parameters": ["temperature"],
                    },
                ]
            }

    def fake_get(url, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(openrouter_models.httpx, "get", fake_get)
    provider = openrouter_models.create_model_provider({})

    models = provider.list_models(environ={"OPENROUTER_API_KEY": "secret"})

    assert captured["headers"] == {
        "Authorization": "Bearer secret",
        "HTTP-Referer": "https://toolang.ai",
        "X-OpenRouter-Title": "Toolang",
        "X-OpenRouter-Categories": "cli-agent",
    }

    assert models == (
        ModelInfo(
            ref="anthropic/claude-sonnet-4",
            provider="openrouter",
            name="claude-sonnet-4",
            model="anthropic/claude-sonnet-4",
            selectors=("claude-sonnet-4", "anthropic/claude-sonnet-4"),
            adapter="chat_completions",
            tools=False,
            streaming=True,
            details="Built-in OpenRouter route.",
        ),
        ModelInfo(
            ref="openai/gpt-5",
            provider="openrouter",
            name="gpt-5",
            model="openai/gpt-5",
            selectors=("gpt-5", "openai/gpt-5"),
            adapter="chat_completions",
            tools=True,
            streaming=True,
            context_window=400000,
            max_output_tokens=128000,
            input_price=0.00000125,
            output_price=0.00001,
            details="General-purpose flagship model.",
        ),
    )


def test_openrouter_provider_prepares_chat_completions_target(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_invoke_chat_completion(target, request):
        captured["target"] = target
        captured["request"] = request
        return ModelCallResult(message=Message.assistant("done"))

    monkeypatch.setattr(chat_completions_models, "invoke_chat_completion", fake_invoke_chat_completion)
    provider = openrouter_models.create_model_provider({})
    adapter = chat_completions_models.create_model_adapter({})
    target = ModelTarget(
        ref="openai/gpt-5",
        provider="openrouter",
        name="gpt-5",
        model="openai/gpt-5",
        adapter="chat_completions",
    )
    request = ModelCall(instructions="dev", messages=[Message.user("hello")])

    result = adapter.invoke(provider.prepare_target(target), request)

    assert result.message == Message.assistant("done")
    assert captured["request"] == request
    assert captured["target"] == ModelTarget(
        ref="openai/gpt-5",
        provider="openrouter",
        name="gpt-5",
        model="openai/gpt-5",
        adapter="chat_completions",
        headers={
            "HTTP-Referer": "https://toolang.ai",
            "X-OpenRouter-Title": "Toolang",
            "X-OpenRouter-Categories": "cli-agent",
        },
    )


def test_chat_completions_adapter_invokes_openai_compatible_client(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Completions:
        def create(self, **payload):
            captured["payload"] = payload
            return SimpleNamespace(
                choices=(
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="done",
                            tool_calls=(
                                SimpleNamespace(
                                    id="call_1",
                                    function=SimpleNamespace(
                                        name="shell__execute",
                                        arguments='{"command":"pwd"}',
                                    ),
                                ),
                            ),
                        )
                    ),
                ),
                usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
            )

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(chat_completions_models, "create_client", lambda target: _Client())
    adapter = chat_completions_models.create_model_adapter({})
    target = ModelTarget(
        ref="deepseek/deepseek-v4-pro",
        provider="deepseek",
        name="deepseek-v4-pro",
        model="deepseek-v4-pro",
        adapter="chat_completions",
        options={"temperature": 0},
    )
    request = ModelCall(
        instructions="dev",
        messages=[Message.user("hello")],
        tools=(
            ToolDefinition(
                name="shell__execute",
                description="Run a shell command.",
                parameters={"type": "object"},
            ),
        ),
    )

    result = adapter.invoke(target, request)

    assert captured["payload"] == {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "dev"},
            {"role": "user", "content": "hello"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "shell__execute",
                    "description": "Run a shell command.",
                    "parameters": {"type": "object"},
                },
            }
        ],
        "temperature": 0,
        "stream": False,
    }
    assert result.message == Message(
        role="assistant",
        parts=(
            Message.assistant("done").parts[0],
            ToolCallPart(
                tool_call_id="call_1",
                call_id="call_1",
                tool_name="shell__execute",
                tool_family="shell__execute",
                input={"command": "pwd"},
            ),
        ),
    )
    assert result.tool_calls == (
        ToolCall(
            tool_call_id="call_1",
            call_id="call_1",
            name="shell__execute",
            input={"command": "pwd"},
        ),
    )
    assert result.usage == ModelUsage(input_tokens=11, output_tokens=7)


def test_chat_completions_adapter_replays_deepseek_reasoning_content() -> None:
    response = SimpleNamespace(
        choices=(
            SimpleNamespace(
                message=SimpleNamespace(
                    content="I need to inspect the directory.",
                    reasoning_content="The user asked for the directory, so list the current folder.",
                    tool_calls=(
                        SimpleNamespace(
                            id="call_1",
                            function=SimpleNamespace(
                                name="filesystem__list",
                                arguments='{"path":"."}',
                            ),
                        ),
                    ),
                )
            ),
        ),
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )

    result = chat_completions_models.parse_chat_completion(response)

    assert result.message is not None
    assert result.message.meta == {
        "reasoning_content": "The user asked for the directory, so list the current folder."
    }

    payload = chat_completions_models.chat_completion_payload(
        ModelTarget(
            ref="deepseek/deepseek-v4-flash",
            provider="deepseek",
            name="deepseek-v4-flash",
            model="deepseek-v4-flash",
            adapter="chat_completions",
        ),
        ModelCall(
            instructions="",
            messages=[
                result.message,
                Message(
                    role="tool",
                    parts=(
                        ToolResultPart(
                            tool_call_id="call_1",
                            call_id="call_1",
                            tool_name="filesystem__list",
                            tool_family="filesystem__list",
                            output={"entries": []},
                        ),
                    ),
                ),
            ],
        ),
        stream=False,
    )

    assert payload["messages"][0]["reasoning_content"] == (
        "The user asked for the directory, so list the current folder."
    )
    assert payload["messages"][0]["tool_calls"][0]["function"]["name"] == "filesystem__list"


def test_chat_completions_adapter_rejects_tool_calls_without_names() -> None:
    raw_tool_calls = (
        SimpleNamespace(
            id=None,
            function=SimpleNamespace(name=None, arguments='{"path":"."}'),
        ),
    )

    with pytest.raises(ToolangError, match="tool call without a function name"):
        chat_completions_models.parse_tool_calls(raw_tool_calls)


def test_chat_completions_stream_rejects_tool_deltas_without_names(monkeypatch) -> None:
    class _Stream:
        def __iter__(self):
            yield SimpleNamespace(
                choices=(
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            tool_calls=(
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    function=SimpleNamespace(name=None, arguments='{"path":"."}'),
                                ),
                            )
                        )
                    ),
                )
            )

        def close(self) -> None:
            return None

    class _Completions:
        def create(self, **payload):
            del payload
            return _Stream()

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(chat_completions_models, "create_client", lambda target: _Client())
    adapter = chat_completions_models.create_model_adapter({})

    with pytest.raises(ToolangError, match="tool call without a function name"):
        adapter.stream(
            ModelTarget(
                ref="deepseek/deepseek-reasoner",
                provider="deepseek",
                name="deepseek-reasoner",
                model="deepseek-reasoner",
                adapter="chat_completions",
            ),
            ModelCall(instructions="", messages=[Message.user("hello")]),
            on_event=lambda _event: None,
        )


def test_chat_completions_stream_collects_deepseek_usage(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Stream:
        def __iter__(self):
            yield SimpleNamespace(
                choices=(
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            reasoning_content="Thinking.",
                            content=None,
                            tool_calls=(),
                        )
                    ),
                ),
                usage=None,
            )
            yield SimpleNamespace(
                choices=(
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            reasoning_content=None,
                            content="done",
                            tool_calls=(),
                        )
                    ),
                ),
                usage=None,
            )
            yield SimpleNamespace(
                choices=(),
                usage=SimpleNamespace(prompt_tokens=13, completion_tokens=8),
            )

        def close(self) -> None:
            return None

    class _Completions:
        def create(self, **payload):
            captured["payload"] = payload
            return _Stream()

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(chat_completions_models, "create_client", lambda target: _Client())
    adapter = chat_completions_models.create_model_adapter({})

    result = adapter.stream(
        ModelTarget(
            ref="deepseek/deepseek-v4-flash",
            provider="deepseek",
            name="deepseek-v4-flash",
            model="deepseek-v4-flash",
            adapter="chat_completions",
        ),
        ModelCall(instructions="", messages=[Message.user("hello")]),
        on_event=lambda _event: None,
    )

    payload = cast(dict[str, object], captured["payload"])

    assert payload["stream_options"] == {"include_usage": True}
    assert result.message == Message.assistant("done")
    assert result.usage == ModelUsage(input_tokens=13, output_tokens=8)


def test_responses_adapter_rejects_openai_audio_inputs_for_non_audio_models(monkeypatch) -> None:
    def fail_invoke_response(*args, **kwargs):
        raise AssertionError("responses.invoke_response should not be called")

    monkeypatch.setattr(responses_models, "invoke_response", fail_invoke_response)
    adapter = responses_models.create_model_adapter({})
    target = ModelTarget(
        ref="openai/gpt-5",
        provider="openai",
        name="gpt-5",
        model="gpt-5",
        adapter="responses",
    )
    request = ModelCall(
        instructions="dev",
        messages=[
            Message(
                role="user",
                parts=(
                    Message.user("hello").parts[0],
                    AudioPart(data="ZGF0YQ==", format="mp3"),
                ),
            )
        ],
    )

    with pytest.raises(ToolangError, match="audio input is not supported for OpenAI model 'gpt-5'"):
        adapter.invoke(target, request)


def test_responses_adapter_rejects_openai_audio_inputs_for_non_audio_models_in_streaming(monkeypatch) -> None:
    def fail_stream_response(*args, **kwargs):
        raise AssertionError("responses.stream_response should not be called")

    monkeypatch.setattr(responses_models, "stream_response", fail_stream_response)
    adapter = responses_models.create_model_adapter({})
    target = ModelTarget(
        ref="openai/gpt-5",
        provider="openai",
        name="gpt-5",
        model="gpt-5",
        adapter="responses",
    )
    request = ModelCall(
        instructions="dev",
        messages=[
            Message(
                role="user",
                parts=(
                    Message.user("hello").parts[0],
                    AudioPart(data="ZGF0YQ==", format="mp3"),
                ),
            )
        ],
    )

    with pytest.raises(ToolangError, match="audio input is not supported for OpenAI model 'gpt-5'"):
        adapter.stream(target, request, on_event=lambda _event: None)


def test_openrouter_provider_preserves_route_header_overrides(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_invoke_chat_completion(target, request):
        captured["target"] = target
        captured["request"] = request
        return ModelCallResult(message=Message.assistant("done"))

    monkeypatch.setattr(chat_completions_models, "invoke_chat_completion", fake_invoke_chat_completion)
    provider = openrouter_models.create_model_provider({})
    adapter = chat_completions_models.create_model_adapter({})
    target = ModelTarget(
        ref="openai/gpt-5",
        provider="openrouter",
        name="gpt-5",
        model="openai/gpt-5",
        adapter="chat_completions",
        headers={
            "http-referer": "https://example.com/toolang-dev",
            "x-openrouter-title": "Toolang Dev",
        },
    )

    adapter.invoke(
        provider.prepare_target(target),
        ModelCall(instructions="dev", messages=[Message.user("hello")]),
    )

    resolved_target = cast(ModelTarget, captured["target"])

    assert resolved_target.headers == {
        "http-referer": "https://example.com/toolang-dev",
        "x-openrouter-title": "Toolang Dev",
        "X-OpenRouter-Categories": "cli-agent",
    }


def test_responses_payload_uses_typed_input_items() -> None:
    payload = response_payload(
        ModelTarget(
            ref="openai/gpt-5",
            provider="openrouter",
            name="gpt-5",
            model="openai/gpt-5",
            adapter="responses",
        ),
        ModelCall(
            instructions="dev",
            messages=[
                Message.user("hello"),
                Message(
                    role="assistant",
                    parts=(
                        ToolCallPart(
                            tool_call_id="fc_1",
                            call_id="call_1",
                            tool_name="shell__execute",
                            tool_family="shell__execute",
                            input={"command": "pwd"},
                        ),
                    ),
                ),
                Message(
                    role="tool",
                    parts=(
                        ToolResultPart(
                            tool_call_id="fc_1",
                            call_id="call_1",
                            tool_name="shell__execute",
                            tool_family="shell__execute",
                            output={"ok": True, "stdout": "/tmp"},
                        ),
                    ),
                ),
                Message.assistant("done"),
            ],
        ),
        stateful=False,
    )

    assert payload["input"] == [
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "dev"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "shell__execute",
            "arguments": '{"command":"pwd"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"ok":true,"name":"shell__execute","output":{"ok":true,"stdout":"/tmp"}}',
        },
        {
            "type": "message",
            "role": "assistant",
            "id": "msg_3",
            "status": "completed",
            "content": [{"type": "output_text", "text": "done"}],
        },
    ]


def test_execute_run_input_reuses_provider_state_for_followups() -> None:
    provider = _FakeModelProvider(
        name="openai",
        responses=[
            ModelCallResult(
                message=Message(
                    role="assistant",
                    parts=(
                        ToolCallPart(
                            tool_call_id="tool-1",
                            call_id="call-1",
                            tool_name="shell__execute",
                            tool_family="shell__execute",
                            input={"command": "pwd"},
                        ),
                    ),
                ),
                tool_calls=(
                    ToolCall(
                        tool_call_id="tool-1",
                        call_id="call-1",
                        name="shell__execute",
                        input={"command": "pwd"},
                    ),
                ),
                state={"previous_response_id": "resp-1", "baseline_count": 2},
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    run_input = _run_input()

    result = load_loop("basic").run(
        RunContext(
            run_input,
            ModelTarget(
                ref="openai/gpt-5",
                provider=provider.name,
                name="gpt-5",
                model="gpt-5",
                adapter="responses",
            ),
            provider,
        )
    )

    assert result.output_text == "done"
    assert provider.requests[0].state is None
    assert provider.requests[1].state == {"previous_response_id": "resp-1", "baseline_count": 2}
    assert [item.to_data() for item in provider.requests[1].messages] == [
        {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "tool_call_id": "tool-1",
                    "call_id": "call-1",
                    "tool_name": "shell__execute",
                    "tool_family": "shell__execute",
                    "input": {"command": "pwd"},
                }
            ],
        },
        {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_result",
                    "tool_call_id": "tool-1",
                    "call_id": "call-1",
                    "tool_name": "shell__execute",
                    "tool_family": "shell__execute",
                    "output": {"ok": True, "stdout": "ran:pwd"},
                }
            ],
        },
    ]


def test_execute_run_input_appends_provider_messages_for_stateless_providers() -> None:
    provider = _FakeModelProvider(
        name="ollama",
        responses=[
            ModelCallResult(
                message=Message(
                    role="assistant",
                    parts=(
                        ToolCallPart(
                            tool_call_id="tool-1",
                            call_id="call-1",
                            tool_name="shell__execute",
                            tool_family="shell__execute",
                            input={"command": "pwd"},
                        ),
                    ),
                ),
                tool_calls=(
                    ToolCall(
                        tool_call_id="tool-1",
                        call_id="call-1",
                        name="shell__execute",
                        input={"command": "pwd"},
                    ),
                ),
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    run_input = _run_input()

    result = load_loop("basic").run(
        RunContext(
            run_input,
            ModelTarget(
                ref="qwen/qwen3",
                provider=provider.name,
                name="qwen3",
                model="qwen3",
                adapter="responses",
            ),
            provider,
        )
    )

    assert result.output_text == "done"
    assert provider.requests[0].state is None
    assert provider.requests[1].state is None
    assert [item.to_data() for item in provider.requests[1].messages] == [
        {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "tool_call_id": "tool-1",
                    "call_id": "call-1",
                    "tool_name": "shell__execute",
                    "tool_family": "shell__execute",
                    "input": {"command": "pwd"},
                }
            ],
        },
        {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_result",
                    "tool_call_id": "tool-1",
                    "call_id": "call-1",
                    "tool_name": "shell__execute",
                    "tool_family": "shell__execute",
                    "output": {"ok": True, "stdout": "ran:pwd"},
                }
            ],
        },
    ]


def test_run_context_omits_tools_for_model_without_tool_support() -> None:
    provider = _FakeModelProvider(
        name="ollama",
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    run_input = _run_input()

    result = load_loop("basic").run(
        RunContext(
            run_input,
            ModelTarget(
                ref="google/gemma4:latest",
                provider=provider.name,
                name="gemma4:latest",
                model="gemma4:latest",
                adapter="responses",
                tools=False,
                streaming=True,
            ),
            provider,
        )
    )

    assert result.output_text == "done"
    assert provider.requests[0].tools == ()

def test_responses_adapter_logs_api_request_and_response_at_debug(caplog, monkeypatch) -> None:
    class _FakeResponse:
        id = "resp_123"
        output_text = "done"
        output = ()
        usage = SimpleNamespace(input_tokens=11, output_tokens=7)
        def model_dump(self, *, mode="json", exclude_none=True) -> dict[str, object]:
            del mode, exclude_none
            return {
                "id": self.id,
                "output_text": self.output_text,
                "usage": {
                    "input_tokens": self.usage.input_tokens,
                    "output_tokens": self.usage.output_tokens,
                },
            }

    captured: dict[str, object] = {}

    class _FakeResponses:
        def create(self, **kwargs):
            captured["payload"] = kwargs
            return _FakeResponse()

    monkeypatch.setattr(
        responses_models,
        "create_client",
        lambda target: SimpleNamespace(responses=_FakeResponses()),
    )
    target = ModelTarget(
        ref="openai/gpt-5",
        provider="openai",
        name="gpt-5",
        model="gpt-5",
        adapter="responses",
        api_key="secret",
        base_url="https://api.openai.com/v1",
        headers={"X-Test": "value"},
    )
    request = ModelCall(
        instructions="Rewrite the input.",
        messages=[Message.user("hello")],
    )

    with caplog.at_level(logging.DEBUG, logger="toolang.model.adapter"):
        result = responses_models.invoke_response(target, request, stateful=True)

    assert result.message == Message.assistant("done")
    assert result.usage == ModelUsage(input_tokens=11, output_tokens=7)
    assert captured["payload"] == response_payload(target, request, stateful=True)
    assert "adapter.request provider=openai ref=openai/gpt-5" in caplog.text
    assert '"model": "gpt-5"' in caplog.text
    assert '"text": "Rewrite the input."' in caplog.text
    assert "adapter.result provider=openai ref=openai/gpt-5" in caplog.text
    assert '"id": "resp_123"' in caplog.text
    assert '"output_text": "done"' in caplog.text
    assert "secret" not in caplog.text


def test_run_context_logs_model_and_tool_io_at_debug(caplog) -> None:
    provider = _FakeModelProvider(
        name="openai",
        responses=[
            ModelCallResult(
                message=Message(
                    role="assistant",
                    parts=(
                        ToolCallPart(
                            tool_call_id="tool-1",
                            call_id="call-1",
                            tool_name="shell__execute",
                            tool_family="shell__execute",
                            input={"command": "pwd"},
                        ),
                    ),
                ),
                tool_calls=(
                    ToolCall(
                        tool_call_id="tool-1",
                        call_id="call-1",
                        name="shell__execute",
                        input={"command": "pwd"},
                    ),
                ),
                usage=ModelUsage(input_tokens=11, output_tokens=7),
                state={"previous_response_id": "resp-1"},
            ),
            ModelCallResult(
                message=Message.assistant("done"),
                usage=ModelUsage(input_tokens=13, output_tokens=3),
            ),
        ],
    )

    with (
        caplog.at_level(logging.DEBUG, logger="toolang.model"),
        caplog.at_level(logging.DEBUG, logger="toolang.tool"),
    ):
        result = load_loop("basic").run(
            RunContext(
                _run_input(),
                ModelTarget(
                    ref="openai/gpt-5",
                    provider=provider.name,
                    name="gpt-5",
                    model="gpt-5",
                    adapter="responses",
                ),
                provider,
            )
        )

    assert result.output_text == "done"
    assert "model.request thread=thread-1 run=run-1 step=1 instructions=" in caplog.text
    assert '"command": "pwd"' in caplog.text
    assert "model.result thread=thread-1 run=run-1 step=1 message=" in caplog.text
    assert '"output_tokens": 7' in caplog.text
    assert "tool.request thread=thread-1 run=run-1 step=2 plugin=" in caplog.text
    assert "tool=shell__execute" in caplog.text
    assert "tool.result thread=thread-1 run=run-1 step=2 plugin=" in caplog.text
    assert '"stdout": "ran:pwd"' in caplog.text


def test_responses_encode_message_preserves_structured_content() -> None:
    encoded = encode_message(Message(role="user", parts=(Message.user("hello").parts[0],)))

    assert encoded == {
        "type": "message",
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": "hello",
            }
        ],
    }


def test_responses_encode_message_supports_multimodal_user_parts() -> None:
    encoded = encode_message(
        Message(
            role="user",
            parts=(
                Message.user("describe this").parts[0],
                ImagePart(image_url="https://example.com/image.png", detail="high"),
                AudioPart(data="ZGF0YQ==", format="mp3"),
                FilePart(file_url="https://example.com/report.pdf", filename="report.pdf"),
            ),
        )
    )

    assert encoded == {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "describe this"},
            {"type": "input_image", "image_url": "https://example.com/image.png", "detail": "high"},
            {"type": "input_audio", "input_audio": {"data": "ZGF0YQ==", "format": "mp3"}},
            {"type": "input_file", "file_url": "https://example.com/report.pdf", "filename": "report.pdf"},
        ],
    }


def test_audio_part_accepts_data_url_in_data_field() -> None:
    part = Message.from_data(
        {
            "role": "user",
            "parts": [
                {
                    "type": "audio",
                    "data": "data:audio/mpeg;base64,ZGF0YQ==",
                }
            ],
        }
    ).parts[0]

    assert isinstance(part, AudioPart)
    assert part.data == "ZGF0YQ=="
    assert part.format == "mp3"
    assert part.media_type == "audio/mpeg"


def test_file_part_preserves_data_url_as_file_data() -> None:
    part = Message.from_data(
        {
            "role": "user",
            "parts": [
                {
                    "type": "file",
                    "data_url": "data:application/pdf;base64,JVBERi0xLjc=",
                    "filename": "report.pdf",
                }
            ],
        }
    ).parts[0]

    assert isinstance(part, FilePart)
    assert part.file_data == "data:application/pdf;base64,JVBERi0xLjc="
    assert part.media_type == "application/pdf"
    assert part.filename == "report.pdf"


def test_responses_reject_non_text_assistant_multimodal_parts() -> None:
    with pytest.raises(ToolangError, match="assistant messages cannot contain image parts"):
        encode_message(
            Message(
                role="assistant",
                parts=(ImagePart(image_url="https://example.com/image.png"),),
            )
        )


def test_responses_skip_historical_tool_items_without_previous_response_id() -> None:
    payload = response_payload(
        ModelTarget(
            ref="openai/gpt-5",
            provider="openai",
            name="gpt-5",
            model="gpt-5",
            adapter="responses",
        ),
        ModelCall(
            instructions="dev",
            messages=[
                Message.user("hello"),
                Message(
                    role="assistant",
                    parts=(
                        ToolCallPart(
                            tool_call_id="fc_1",
                            call_id="call_1",
                            tool_name="shell__execute",
                            tool_family="shell__execute",
                            input={"command": "pwd"},
                        ),
                    ),
                ),
                Message(
                    role="tool",
                    parts=(
                        ToolResultPart(
                            tool_call_id="fc_1",
                            call_id="call_1",
                            tool_name="shell__execute",
                            tool_family="shell__execute",
                            output={"ok": True, "stdout": "/tmp"},
                        ),
                    ),
                ),
                Message.assistant("done"),
            ],
        ),
        stateful=True,
    )

    assert "previous_response_id" not in payload
    assert payload["input"] == [
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "dev"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "id": "msg_3",
            "status": "completed",
            "content": [{"type": "output_text", "text": "done"}],
        },
    ]


def test_responses_previous_response_id_replays_tool_output_without_item_id() -> None:
    payload = response_payload(
        ModelTarget(
            ref="openai/gpt-5",
            provider="openai",
            name="gpt-5",
            model="gpt-5",
            adapter="responses",
        ),
        ModelCall(
            instructions="dev",
            messages=[
                Message.user("hello"),
                Message(
                    role="assistant",
                    parts=(
                        ToolCallPart(
                            tool_call_id="fc_1",
                            call_id="call_1",
                            tool_name="shell__execute",
                            tool_family="shell__execute",
                            input={"command": "pwd"},
                        ),
                    ),
                ),
                Message(
                    role="tool",
                    parts=(
                        ToolResultPart(
                            tool_call_id="fc_1",
                            call_id="call_1",
                            tool_name="shell__execute",
                            tool_family="shell__execute",
                            output={"ok": True, "stdout": "/tmp"},
                        ),
                    ),
                ),
            ],
            state={"previous_response_id": "resp_1", "baseline_count": 2},
        ),
        stateful=True,
    )

    assert payload["previous_response_id"] == "resp_1"
    assert payload["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"ok":true,"name":"shell__execute","output":{"ok":true,"stdout":"/tmp"}}',
        }
    ]


def _run_input() -> RunInput:
    tool = _FakeTool()
    live = SimpleNamespace(
        program=SimpleNamespace(
            source_text="agent alice\n\nthunk:\n  Reply directly.\n",
            prepared=SimpleNamespace(agent_name="alice"),
        ),
        fingerprint="live-1",
    )
    return RunInput(
        run=RunBinding(
            run_id="run-1",
            group="chat",
            origin="chat",
            thread_id="thread-1",
            thunk_name=None,
            input_text="hello",
            message=Message.user("hello"),
            model_selector=None,
            run_loop="basic",
            metadata={},
            live=cast(Any, live),
            created_at="2026-04-10T00:00:00Z",
        ),
        thunk=Thunk(
            name="main",
            input=ParamDecl(name="_"),
            messages=(MessageBlock(kind="user", text="Reply directly.", span=SourceSpan(1)),),
        ),
        input_text="hello",
        message=Message.user("hello"),
        context_text="",
        params={},
        user_template_context={},
        system_template_context={},
        history=(),
        models_base=("openai/gpt-5",),
        tools_base={tool.name: tool},
        snapshot=RunSnapshot(
            agent=SnapshotAgent(name="alice", root="/tmp/root", home="/tmp/home"),
            run=SnapshotRun(
                run_id="run-1",
                group="chat",
                origin="chat",
                thread_id="thread-1",
                run_loop="basic",
                live_fingerprint="",
            ),
            program=SnapshotProgram(source_path="", thunk={}),
        ),
        debug={},
    )
