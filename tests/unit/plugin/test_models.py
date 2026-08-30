from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
import logging
from pathlib import Path
import tomllib
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.base.protocols.model import ModelAdapter
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.model import (
    ModelInfo,
    ModelParameters,
    ModelTarget,
    Provider,
    ReasoningParameters,
    ResolvedProvider,
)
from toolang.base.types.policy import RunBindings
from toolang.base.types.run import ModelCall, ModelCallResult, ModelUsage, ToolCall
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.execution.events import RunEvent, StepEnd
from toolang.execution.executor.common import BoundRun
from toolang.execution.executor.prepare import _AgicFrame
from toolang.execution.executor.runs.agic import _AgicState, _execute
from toolang.execution.tools.runtime import runtime_tools
from toolang.execution.records import ControlRecord, SteerControlPayload
from toolang.execution.types import ControlRef, Local
from toolang.plugin.models.discovery import missing_provider_env_vars
from toolang.plugin.models.resolution import (
    apply_model_parameters,
    model_reasoning_efforts,
    resolve_model,
    resolve_model_ref,
    select_model_selectors,
)
from toolang.plugin.models.views import _format_decimal_unit
from toolang.setup import AgentSetup
from toolang.plugin.models.catalog import (
    PACKAGED_MODEL_CATALOG,
    read_model_catalog_snapshot,
)
from toolang.plugin.models.loading import load_model_adapters
from toolang.plugin.models.adapters import chat_completions as chat_completions_models
from toolang.plugin.models.adapters import messages as messages_models
from toolang.plugin.models.adapters import responses as responses_models
from toolang.plugin.models.adapters.responses import encode_message, response_payload
from toolang.lang.ast import AgicDecl, Message as AstMessage, Parameter, Program, Span
from toolang.lang.input import RunnableInput
from toolang.plugin.models.config import parse_default_models, parse_model_aliases


def load_config_layers(root: Path, agent_name: str) -> tuple[dict[str, object], ...]:
    layers: list[dict[str, object]] = []
    for path in (root / "config.toml", root / "agents" / agent_name / "config.toml"):
        if path.is_file():
            layers.append(tomllib.loads(path.read_text(encoding="utf-8")))
    return tuple(layers)


class _FakeTool(AgentTool):
    name = "shell__execute"
    plugin_name = "shell"
    toolset = "shell"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Run a shell command.",
            parameters={"type": "object"},
        )

    async def invoke(self, arguments, context: ToolContext) -> dict[str, Any]:
        del context
        return {"ok": True, "stdout": f"ran:{arguments['command']}"}


class _FakeModels(ModelAdapter):
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
        self.default_api = default_base_url
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

    def catalog_provider(self, *, environ: dict[str, str]) -> Provider:
        env = self._required_env_vars or (
            (self._default_api_key_env,) if self._default_api_key_env else ()
        )
        endpoint = self._default_base_url or "https://example.invalid/v1"
        adapter = (
            self._models[0].adapter
            if self._models
            else "chat_completions"
            if self.name in {"deepseek", "google", "openrouter"}
            else "responses"
        )
        return Provider(
            id=self.name,
            name=self.name,
            env=env,
            npm="@ai-sdk/openai-compatible",
            api=self._default_base_url,
            models={},
            resolved=ResolvedProvider(
                adapter=adapter,
                api=endpoint,
                env=(tuple(env),) if len(env) > 1 else env,
                ready=all(str(environ.get(name, "")).strip() for name in env),
            ),
        )

    async def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        del target
        self.requests.append(request)
        return self._responses.pop(0)

    async def stream(
        self, target: ModelTarget, request: ModelCall, *, on_event
    ) -> ModelCallResult:
        del on_event
        return await self.invoke(target, request)


class _SelectionContext:
    """Adapt concise test fixtures to the immutable model snapshot contract."""

    def __init__(
        self,
        *,
        model_providers: dict[str, _FakeModels],
        model_aliases: dict[str, Any],
        default_models: tuple[str, ...],
        model_environ: dict[str, str],
        **_ignored: object,
    ) -> None:
        self.providers = {
            name: provider.catalog_provider(environ=model_environ)
            for name, provider in model_providers.items()
        }
        self.model_aliases = model_aliases
        self.default_models = default_models
        self.envs = model_environ
        self.models = tuple(
            model
            for name, provider in model_providers.items()
            if not missing_provider_env_vars(self.providers[name], environ=self.envs)
            for model in provider.list_models(environ=self.envs)
        )


async def _ignore_event(_event: object) -> None:
    return None


def test_model_resolution_resolves_named_route(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    (toolang_root / "agents" / "alice").mkdir(parents=True, exist_ok=True)
    (toolang_root / "config.toml").write_text(
        "[models]\n"
        'default = ["fast"]\n'
        "\n"
        "[models.aliases.fast]\n"
        'ref = "openai/gpt-5"\n'
        'provider = "openai"\n',
        encoding="utf-8",
    )
    provider = _FakeModels(
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
    context = _SelectionContext(
        model_providers={"openai": provider},
        model_aliases=parse_model_aliases(load_config_layers(toolang_root, "alice")),
        default_models=parse_default_models(load_config_layers(toolang_root, "alice")),
        model_environ={"OPENAI_API_KEY": "secret"},
    )

    target = resolve_model(context, selector="fast")

    assert target.ref == "openai/gpt-5"
    assert target.provider == "openai"
    assert target.model == "gpt-5"
    assert target.api_key == "secret"


def test_model_resolution_resolves_explicit_provider_route() -> None:
    provider = _FakeModels(
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
    context = _SelectionContext(
        model_providers={"openrouter": provider},
        model_aliases={},
        default_models=(),
        model_environ={},
    )

    target = resolve_model(context, selector="openai/gpt-5[openrouter]")

    assert target.provider == "openrouter"
    assert target.model == "openai/gpt-5"


def test_model_reasoning_parameters_use_catalog_order_and_replace_defaults() -> None:
    provider = _FakeModels(
        name="openai",
        models=(
            ModelInfo(
                ref="openai/gpt-5",
                provider="openai",
                name="GPT-5",
                model="gpt-5",
                adapter="responses",
                metadata={
                    "reasoning_options": [
                        {"type": "toggle"},
                        {
                            "type": "effort",
                            "values": ["medium", "high", "medium", "future"],
                        },
                        {"type": "budget", "values": [1024]},
                    ]
                },
            ),
        ),
    )
    context = _SelectionContext(
        model_providers={"openai": provider},
        model_aliases={},
        default_models=(),
        model_environ={},
    )
    target = replace(
        resolve_model(context, selector="openai/gpt-5"),
        reasoning={"enabled": True, "effort": "medium"},
    )

    assert model_reasoning_efforts(context, target) == ("medium", "high")
    assert apply_model_parameters(context, target, ModelParameters()) == target
    selected = apply_model_parameters(
        context,
        target,
        ModelParameters(ReasoningParameters("high")),
    )
    assert selected.reasoning == {"effort": "high"}
    with pytest.raises(ToolangError, match="allowed: medium, high"):
        apply_model_parameters(
            context,
            target,
            ModelParameters(ReasoningParameters("max")),
        )


def test_model_resolution_prefers_an_exact_route_over_target_identity() -> None:
    context = _SelectionContext(
        model_providers={
            "openai": _FakeModels(
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
            "openrouter": _FakeModels(
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

    assert resolve_model_ref(context, selector="openai/gpt-5") == "openai/gpt-5"
    assert resolve_model(context, selector="openai/gpt-5").provider == "openai"
    assert (
        resolve_model_ref(context, selector="openai/gpt-5[openrouter]")
        == "openrouter/openai/gpt-5"
    )
    with pytest.raises(ToolangError, match="ambiguous"):
        resolve_model(context, selector="gpt-5")


def test_model_resolution_rejects_missing_provider_env_before_target_use() -> None:
    provider = _FakeModels(
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
    context = _SelectionContext(
        model_providers={"openai": provider},
        model_aliases={},
        default_models=(),
        model_environ={},
    )

    with pytest.raises(ToolangError, match="OPENAI_API_KEY"):
        resolve_model(context, selector="openai/gpt-5[openai]")


def test_model_resolution_skips_unconfigured_provider_when_configured_match_exists() -> (
    None
):
    openai = _FakeModels(
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
    openrouter = _FakeModels(
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
    context = _SelectionContext(
        model_providers={"openai": openai, "openrouter": openrouter},
        model_aliases={},
        default_models=(),
        model_environ={"OPENROUTER_API_KEY": "secret"},
    )

    target = resolve_model(context, selector="gpt-5")

    assert target.provider == "openrouter"


def test_model_resolution_uses_first_allowed_selector_as_default() -> None:
    provider = _FakeModels(
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
    context = _SelectionContext(
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
    provider = _FakeModels(
        name="openrouter",
        models=(
            ModelInfo(
                ref="openai/gpt-5",
                provider="openrouter",
                name="gpt-5",
                model="gpt-5",
                selectors=("gpt-5",),
                adapter="responses",
            ),
            ModelInfo(
                ref="openai/o3",
                provider="openrouter",
                name="o3",
                model="o3",
                selectors=("o3",),
                adapter="responses",
            ),
        ),
    )
    context = _SelectionContext(
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
    provider = _FakeModels(
        name="openrouter",
        models=(
            ModelInfo(
                ref="openai/gpt-5",
                provider="openrouter",
                name="gpt-5",
                model="gpt-5",
                selectors=("gpt-5",),
                adapter="responses",
            ),
            ModelInfo(
                ref="openai/o3",
                provider="openrouter",
                name="o3",
                model="o3",
                selectors=("o3",),
                adapter="responses",
            ),
        ),
    )
    context = _SelectionContext(
        model_providers={"openrouter": provider},
        model_aliases={},
        default_models=(),
        model_environ={},
    )

    with pytest.raises(ToolangError, match="outside the current resources") as exc:
        resolve_model(
            context,
            selector="o3[openrouter]",
            default_selector="gpt-5[openrouter]",
            allowed_selectors=("gpt-5[openrouter]",),
        )
    message = str(exc.value)
    assert "o3[openrouter]" in message
    assert "allowed: openrouter/openai/gpt-5" in message
    assert "[openrouter]" not in message.partition("(allowed: ")[2]


def test_model_resolution_reports_no_matched_models_when_selector_misses() -> None:
    provider = _FakeModels(
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
    context = _SelectionContext(
        model_providers={"openrouter": provider},
        model_aliases={},
        default_models=(),
        model_environ={"OPENROUTER_API_KEY": "secret"},
    )

    with pytest.raises(ToolangError, match="No matched models."):
        resolve_model(context, selector="anthropic/claude-sonnet-4.5")


def test_select_model_selectors_preserves_allowed_order_for_intersection() -> None:
    provider = _FakeModels(
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
    context = _SelectionContext(
        model_providers={"openrouter": provider},
        model_aliases={},
        default_models=(),
        model_environ={},
    )

    selectors = select_model_selectors(
        context,
        directive_selectors=("openai/gpt-5", "openai/o3"),
        allowed_selectors=("openai/o3[openrouter]", "openai/gpt-5[openrouter]"),
    )

    assert selectors == ("openrouter/openai/o3", "openrouter/openai/gpt-5")


def test_select_model_selectors_supports_name_glob_without_matching_family() -> None:
    provider = _FakeModels(
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
                ref="anthropic/claude-sonnet",
                provider="openrouter",
                name="claude-sonnet",
                model="claude-sonnet",
                selectors=("claude-sonnet", "anthropic/claude-sonnet"),
                adapter="responses",
            ),
        ),
    )
    context = _SelectionContext(
        model_providers={"openrouter": provider},
        model_aliases={},
        default_models=(),
        model_environ={},
    )

    assert select_model_selectors(context, allowed_selectors=("gpt-*",)) == (
        "openrouter/openai/gpt-5",
    )
    assert select_model_selectors(context, allowed_selectors=("openai/*",)) == (
        "openrouter/openai/gpt-5",
    )
    with pytest.raises(ToolangError, match="No matched models."):
        select_model_selectors(context, allowed_selectors=("openai",))


def test_select_model_selectors_expands_route_neutral_agic_refs_from_discovery() -> (
    None
):
    openai = _FakeModels(
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
            ModelInfo(
                ref="openai/o3",
                provider="openai",
                name="o3",
                model="o3",
                selectors=("o3", "openai/o3"),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENAI_API_KEY",),
    )
    openrouter = _FakeModels(
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
                ref="openai/o3",
                provider="openrouter",
                name="o3",
                model="openai/o3",
                selectors=("o3", "openai/o3"),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENROUTER_API_KEY",),
    )
    context = _SelectionContext(
        model_providers={"openai": openai, "openrouter": openrouter},
        model_aliases={},
        default_models=(),
        model_environ={"OPENAI_API_KEY": "secret", "OPENROUTER_API_KEY": "secret"},
    )

    selectors = select_model_selectors(
        context,
        directive_selectors=("openai/o3", "openai/gpt-5"),
    )

    assert selectors == (
        "openai/o3",
        "openrouter/openai/o3",
        "openai/gpt-5",
        "openrouter/openai/gpt-5",
    )


def test_select_model_selectors_skips_providers_missing_required_env() -> None:
    openai = _FakeModels(
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
    openrouter = _FakeModels(
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
    context = _SelectionContext(
        model_providers={"openai": openai, "openrouter": openrouter},
        model_aliases={},
        default_models=(),
        model_environ={"OPENROUTER_API_KEY": "secret"},
    )
    selectors = select_model_selectors(
        context,
        directive_selectors=("openai/gpt-5",),
    )

    assert selectors == ("openrouter/openai/gpt-5",)


def test_select_model_selectors_prefers_exact_ref_over_version_aliases() -> None:
    openrouter = _FakeModels(
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
                selectors=(
                    "gpt-5-2025-08-07",
                    "openai/gpt-5-2025-08-07",
                    "openai/gpt-5",
                ),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENROUTER_API_KEY",),
    )
    context = _SelectionContext(
        model_providers={"openrouter": openrouter},
        model_aliases={},
        default_models=(),
        model_environ={"OPENROUTER_API_KEY": "secret"},
    )

    selectors = select_model_selectors(
        context,
        directive_selectors=("openai/gpt-5",),
    )

    assert selectors == ("openrouter/openai/gpt-5",)


def test_select_model_selectors_returns_all_discoverable_when_unrestricted() -> None:
    openai = _FakeModels(
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
            ModelInfo(
                ref="openai/o3",
                provider="openai",
                name="o3",
                model="o3",
                selectors=("o3", "openai/o3"),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENAI_API_KEY",),
    )
    openrouter = _FakeModels(
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
                ref="openai/o3",
                provider="openrouter",
                name="o3",
                model="openai/o3",
                selectors=("o3", "openai/o3"),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENROUTER_API_KEY",),
    )
    context = _SelectionContext(
        model_providers={"openai": openai, "openrouter": openrouter},
        model_aliases={},
        default_models=(),
        model_environ={"OPENAI_API_KEY": "secret", "OPENROUTER_API_KEY": "secret"},
    )

    selectors = select_model_selectors(context)

    assert selectors == (
        "openai/gpt-5",
        "openrouter/openai/gpt-5",
        "openai/o3",
        "openrouter/openai/o3",
    )


def test_model_resolution_only_reads_captured_model_snapshot() -> None:
    openrouter = _FakeModels(
        name="openrouter",
        models=(
            ModelInfo(
                ref="anthropic/claude-4.5-sonnet-20250929",
                provider="openrouter",
                name="claude-4.5-sonnet-20250929",
                model="anthropic/claude-sonnet-4.5",
                selectors=(
                    "anthropic/claude-sonnet-4.5",
                    "anthropic/claude-4.5-sonnet-20250929",
                ),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENROUTER_API_KEY",),
    )
    context = _SelectionContext(
        model_providers={"openrouter": openrouter},
        model_aliases={},
        default_models=(),
        model_environ={"OPENROUTER_API_KEY": "secret"},
    )
    discovery_calls = openrouter.list_models_calls

    selectors = select_model_selectors(
        context,
        directive_selectors=("anthropic/claude-4.5-sonnet-20250929",),
    )
    target = resolve_model(context, selector=selectors[0])

    assert selectors == ("openrouter/anthropic/claude-4.5-sonnet-20250929",)
    assert target.ref == "anthropic/claude-4.5-sonnet-20250929"
    assert openrouter.list_models_calls == discovery_calls


def test_model_selection_filters_the_complete_captured_snapshot() -> None:
    openai = _FakeModels(
        name="openai",
        models=(
            ModelInfo(
                ref="openai/gpt-5",
                provider="openai",
                name="gpt-5",
                model="gpt-5",
                selectors=("gpt-5",),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENAI_API_KEY",),
    )
    openrouter = _FakeModels(
        name="openrouter",
        models=(
            ModelInfo(
                ref="anthropic/claude-sonnet-4.5",
                provider="openrouter",
                name="claude-sonnet-4.5",
                model="anthropic/claude-sonnet-4.5",
                selectors=("claude",),
                adapter="responses",
            ),
        ),
        required_env_vars=("OPENROUTER_API_KEY",),
    )
    context = _SelectionContext(
        model_providers={"openai": openai, "openrouter": openrouter},
        model_aliases={},
        default_models=(),
        model_environ={"OPENAI_API_KEY": "secret", "OPENROUTER_API_KEY": "secret"},
    )
    discovery_calls = (openai.list_models_calls, openrouter.list_models_calls)

    selectors = select_model_selectors(
        context, allowed_selectors=("openai/gpt-5[openai]",)
    )

    assert selectors == ("openai/gpt-5",)
    assert (openai.list_models_calls, openrouter.list_models_calls) == discovery_calls


def test_model_route_can_override_provider_defaults(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    (toolang_root / "agents" / "alice").mkdir(parents=True, exist_ok=True)
    (toolang_root / "config.toml").write_text(
        "[models.aliases.gateway]\n"
        'ref = "openai/gpt-5"\n'
        'provider = "openai"\n'
        'adapter = "responses"\n'
        'endpoint = "https://gateway.example.com/v1"\n'
        'key_env = "GATEWAY_API_KEY"\n'
        'headers = { "X-Team" = "infra" }\n',
        encoding="utf-8",
    )
    provider = _FakeModels(
        name="openai",
        models=(),
        default_base_url="https://api.openai.com/v1",
        default_api_key_env="OPENAI_API_KEY",
    )
    context = _SelectionContext(
        model_providers={"openai": provider},
        model_aliases=parse_model_aliases(load_config_layers(toolang_root, "alice")),
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
        '[models.aliases.qwen]\nref = "qwen/qwen3-coder"\nprovider = "openrouter"\n',
        encoding="utf-8",
    )
    provider = _FakeModels(
        name="openrouter",
        models=(),
        default_base_url="https://openrouter.ai/api/v1",
        default_api_key_env="OPENROUTER_API_KEY",
    )
    context = _SelectionContext(
        model_providers={"openrouter": provider},
        model_aliases=parse_model_aliases(load_config_layers(toolang_root, "alice")),
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
        "[models.aliases.gateway]\n"
        'ref = "openai/gpt-5"\n'
        'provider = "openai"\n'
        'adapter = "responses"\n'
        'key_env = "GATEWAY_API_KEY"\n',
        encoding="utf-8",
    )
    provider = _FakeModels(
        name="openai",
        models=(),
        required_env_vars=("OPENAI_API_KEY",),
        default_api_key_env="OPENAI_API_KEY",
    )
    context = _SelectionContext(
        model_providers={"openai": provider},
        model_aliases=parse_model_aliases(load_config_layers(toolang_root, "alice")),
        default_models=(),
        model_environ={},
    )

    with pytest.raises(ToolangError, match="model alias 'gateway'.*GATEWAY_API_KEY"):
        resolve_model(context, selector="gateway")


def test_packaged_catalog_includes_mainstream_remote_providers() -> None:
    snapshot = read_model_catalog_snapshot(PACKAGED_MODEL_CATALOG)

    assert {"anthropic", "deepseek", "google", "openai", "openrouter"} <= set(
        snapshot.providers
    )
    assert snapshot.providers["deepseek"].env == ("DEEPSEEK_API_KEY",)
    assert "GOOGLE_GENERATIVE_AI_API_KEY" in snapshot.providers["google"].env
    assert snapshot.providers["openrouter"].env == ("OPENROUTER_API_KEY",)


def test_package_registers_catalogs_without_legacy_model_provider_entry_points() -> (
    None
):
    pyproject = tomllib.loads(
        (Path(__file__).parents[3] / "pyproject.toml").read_text(encoding="utf-8")
    )
    entry_points = pyproject["project"]["entry-points"]

    assert "toolang.model_provider" not in entry_points
    assert entry_points["toolang.model_catalog"] == {
        "models_dev": "toolang.plugin.models.catalog:create_models_dev_model_catalog",
        "ollama": "toolang.plugin.models.local:create_ollama_model_catalog",
        "llama_cpp": "toolang.plugin.models.local:create_llama_cpp_model_catalog",
    }


def test_builtin_model_adapter_loader_includes_all_protocol_adapters() -> None:
    adapters = load_model_adapters()

    assert tuple(sorted(adapters)) == (
        "chat_completions",
        "generate_content",
        "messages",
        "responses",
    )


def test_decimal_unit_formatting_accepts_integer_values() -> None:
    assert _format_decimal_unit(1) == "1"


def test_messages_adapter_replays_signed_thinking_before_tool_use() -> None:
    result = messages_models.parse_message_response(
        {
            "content": [
                {
                    "type": "thinking",
                    "thinking": "I should inspect the files.",
                    "signature": "signed-thinking",
                },
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "fs__list",
                    "input": {"path": "."},
                },
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )
    assert result.message is not None

    payload = messages_models.messages_payload(
        ModelTarget(
            ref="anthropic/claude",
            provider="anthropic",
            name="Claude",
            model="claude",
            adapter="messages",
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
                            tool_name="fs__list",
                            tool_family="fs__list",
                            output={"entries": []},
                        ),
                    ),
                ),
            ],
            continuation=result.continuation,
        ),
        stream=False,
    )

    assistant = cast(list[dict[str, object]], payload["messages"])[0]
    content = cast(list[dict[str, object]], assistant["content"])
    assert content[0] == {
        "type": "thinking",
        "thinking": "I should inspect the files.",
        "signature": "signed-thinking",
    }
    assert content[1]["type"] == "tool_use"
    assert content[1]["id"] == "call_1"


def test_messages_adapter_keeps_thinking_budget_below_max_tokens() -> None:
    target = ModelTarget(
        ref="anthropic/claude",
        provider="anthropic",
        name="Claude",
        model="claude",
        adapter="messages",
        reasoning={"budget_tokens": 8_000},
    )
    request = ModelCall(instructions="", messages=[Message.user("hello")])

    payload = messages_models.messages_payload(target, request, stream=False)

    assert payload["max_tokens"] == 8_001
    with pytest.raises(ToolangError, match="lower than max_tokens"):
        messages_models.messages_payload(
            ModelTarget(
                ref=target.ref,
                provider=target.provider,
                name=target.name,
                model=target.model,
                adapter=target.adapter,
                options={"max_tokens": 4_096},
                reasoning=target.reasoning,
            ),
            request,
            stream=False,
        )


def test_chat_completions_adapter_invokes_openai_compatible_client(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Completions:
        async def create(self, **payload):
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

    monkeypatch.setattr(
        chat_completions_models, "create_client", lambda target: _Client()
    )
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

    result = asyncio.run(adapter.invoke(target, request))

    assert captured["payload"] == {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "dev"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            },
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
                                name="fs__list",
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
    call_part = next(
        part for part in result.message.parts if isinstance(part, ToolCallPart)
    )
    assert call_part.reasoning == (
        "The user asked for the directory, so list the current folder."
    )

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
                            tool_name="fs__list",
                            tool_family="fs__list",
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
    assert payload["messages"][0]["tool_calls"][0]["function"]["name"] == "fs__list"


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
        async def __aiter__(self):
            yield SimpleNamespace(
                choices=(
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            tool_calls=(
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    function=SimpleNamespace(
                                        name=None, arguments='{"path":"."}'
                                    ),
                                ),
                            )
                        )
                    ),
                )
            )

        async def close(self) -> None:
            return None

    class _Completions:
        async def create(self, **payload):
            del payload
            return _Stream()

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(
        chat_completions_models, "create_client", lambda target: _Client()
    )
    adapter = chat_completions_models.create_model_adapter({})

    with pytest.raises(ToolangError, match="tool call without a function name"):
        asyncio.run(
            adapter.stream(
                ModelTarget(
                    ref="deepseek/deepseek-reasoner",
                    provider="deepseek",
                    name="deepseek-reasoner",
                    model="deepseek-reasoner",
                    adapter="chat_completions",
                ),
                ModelCall(instructions="", messages=[Message.user("hello")]),
                on_event=_ignore_event,
            )
        )


@pytest.mark.parametrize("provider", ["deepseek", "ollama", "llama_cpp"])
def test_chat_completions_stream_collects_usage(monkeypatch, provider: str) -> None:
    captured: dict[str, object] = {}
    events: list[str] = []

    async def record_event(event: object) -> None:
        await asyncio.sleep(0)
        events.append(type(event).__name__)

    class _Stream:
        async def __aiter__(self):
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

        async def close(self) -> None:
            return None

    class _Completions:
        async def create(self, **payload):
            captured["payload"] = payload
            return _Stream()

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(
        chat_completions_models, "create_client", lambda target: _Client()
    )
    adapter = chat_completions_models.create_model_adapter({})

    result = asyncio.run(
        adapter.stream(
            ModelTarget(
                ref=f"{provider}/test-model",
                provider=provider,
                name="test-model",
                model="test-model",
                adapter="chat_completions",
            ),
            ModelCall(instructions="", messages=[Message.user("hello")]),
            on_event=record_event,
        )
    )

    payload = cast(dict[str, object], captured["payload"])

    assert payload["stream_options"] == {"include_usage": True}
    assert result.message == Message.assistant("done")
    assert result.usage == ModelUsage(input_tokens=13, output_tokens=8)
    assert events == ["ModelPartStart", "ModelPartDelta", "ModelPartEnd"]


def test_responses_adapter_rejects_openai_audio_inputs_for_non_audio_models(
    monkeypatch,
) -> None:
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

    with pytest.raises(
        ToolangError, match="audio input is not supported for OpenAI model 'gpt-5'"
    ):
        asyncio.run(adapter.invoke(target, request))


def test_responses_adapter_rejects_openai_audio_inputs_for_non_audio_models_in_streaming(
    monkeypatch,
) -> None:
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

    with pytest.raises(
        ToolangError, match="audio input is not supported for OpenAI model 'gpt-5'"
    ):
        asyncio.run(adapter.stream(target, request, on_event=_ignore_event))


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


def test_protocol_payloads_apply_normalized_reasoning_controls() -> None:
    request = ModelCall(instructions="", messages=[Message.user("hello")])
    target = ModelTarget(
        ref="openai/gpt-5",
        provider="openai",
        name="gpt-5",
        model="gpt-5",
        adapter="responses",
        reasoning={"effort": "high"},
    )

    responses_payload = response_payload(target, request, stateful=False)
    chat_payload = chat_completions_models.chat_completion_payload(
        target,
        request,
        stream=False,
    )

    assert responses_payload["reasoning"] == {"effort": "high"}
    assert chat_payload["reasoning_effort"] == "high"


def test_protocol_usage_normalizes_cache_reasoning_audio_and_reported_cost() -> None:
    chat = chat_completions_models.chat_usage(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=40,
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=60,
                    audio_tokens=10,
                ),
                completion_tokens_details=SimpleNamespace(
                    reasoning_tokens=30,
                    audio_tokens=5,
                ),
                cost="0.03",
            )
        )
    )
    responses = responses_models.response_usage(
        SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=40,
                input_tokens_details=SimpleNamespace(cached_tokens=60),
                output_tokens_details=SimpleNamespace(reasoning_tokens=30),
            )
        )
    )

    assert chat == ModelUsage(
        input_tokens=100,
        output_tokens=40,
        input_uncached_tokens=40,
        input_cache_read_tokens=60,
        input_audio_tokens=10,
        output_visible_tokens=10,
        output_reasoning_tokens=30,
        output_audio_tokens=5,
        reported_cost=Decimal("0.03"),
        reported_currency="USD",
    )
    assert responses == ModelUsage(
        input_tokens=100,
        output_tokens=40,
        input_uncached_tokens=40,
        input_cache_read_tokens=60,
        output_visible_tokens=10,
        output_reasoning_tokens=30,
    )


def test_execute_run_input_reuses_provider_state_for_followups() -> None:
    provider = _FakeModels(
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
                continuation={"previous_response_id": "resp-1", "baseline_count": 2},
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    model = ModelTarget(
        ref="openai/gpt-5",
        provider=provider.name,
        name="gpt-5",
        model="gpt-5",
        adapter="responses",
    )

    result = _run_agic(_prepared_agic(provider, model))

    assert result == Message.assistant("done")
    assert provider.requests[0].continuation is None
    assert provider.requests[1].continuation == {
        "previous_response_id": "resp-1",
        "baseline_count": 2,
    }
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
    provider = _FakeModels(
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
    model = ModelTarget(
        ref="qwen/qwen3",
        provider=provider.name,
        name="qwen3",
        model="qwen3",
        adapter="responses",
    )

    result = _run_agic(_prepared_agic(provider, model))

    assert result == Message.assistant("done")
    assert provider.requests[0].continuation is None
    assert provider.requests[1].continuation is None
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


def test_agic_omits_tools_for_model_without_tool_support() -> None:
    provider = _FakeModels(
        name="ollama",
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    model = ModelTarget(
        ref="google/gemma4:latest",
        provider=provider.name,
        name="gemma4:latest",
        model="gemma4:latest",
        adapter="responses",
        tools=False,
        streaming=True,
    )

    result = _run_agic(
        replace(_prepared_agic(provider, model), runtime_tools=runtime_tools())
    )

    assert result == Message.assistant("done")
    assert provider.requests[0].tools == ()


def test_responses_adapter_logs_api_request_and_response_at_debug(
    caplog, monkeypatch
) -> None:
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
        async def create(self, **kwargs):
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

    with caplog.at_level(
        logging.DEBUG,
        logger="toolang.plugin.models.adapters.responses",
    ):
        result = asyncio.run(
            responses_models.invoke_response(target, request, stateful=True)
        )

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


def test_agic_logs_model_and_tool_io_at_debug(caplog) -> None:
    provider = _FakeModels(
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
                continuation={"previous_response_id": "resp-1"},
            ),
            ModelCallResult(
                message=Message.assistant("done"),
                usage=ModelUsage(input_tokens=13, output_tokens=3),
            ),
        ],
    )

    with caplog.at_level(
        logging.DEBUG,
        logger="toolang.execution.executor.diagnostics",
    ):
        result = _run_agic(
            _prepared_agic(
                provider,
                ModelTarget(
                    ref="openai/gpt-5",
                    provider=provider.name,
                    name="gpt-5",
                    model="gpt-5",
                    adapter="responses",
                ),
            )
        )

    assert result == Message.assistant("done")
    assert "model.request thread=thread-1 run=run_1 step=0 instructions=" in caplog.text
    assert '"command": "pwd"' in caplog.text
    assert "model.result thread=thread-1 run=run_1 step=0 message=" in caplog.text
    assert '"output_tokens": 7' in caplog.text
    assert "tool.request thread=thread-1 run=run_1 step=1 plugin=" in caplog.text
    assert "tool=shell__execute" in caplog.text
    assert "tool.result thread=thread-1 run=run_1 step=1 plugin=" in caplog.text
    assert '"stdout": "ran:pwd"' in caplog.text


def test_chat_completions_encode_multimodal_user_parts() -> None:
    encoded = chat_completions_models.encode_message(
        ModelTarget(
            ref="openai/gpt-audio",
            provider="openai",
            name="gpt-audio",
            model="gpt-audio",
            adapter="chat_completions",
        ),
        Message(
            role="user",
            parts=(
                Message.user("describe").parts[0],
                ImagePart(
                    image_url="https://example.com/image.png",
                    detail="high",
                ),
                AudioPart(data="ZGF0YQ==", format="mp3"),
                DocumentPart(
                    data="data:application/pdf;base64,JVBERi0xLjc=",
                    filename="report.pdf",
                ),
            ),
        ),
    )

    assert encoded == {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe"},
            {
                "type": "image_url",
                "image_url": {
                    "url": "https://example.com/image.png",
                    "detail": "high",
                },
            },
            {
                "type": "input_audio",
                "input_audio": {"data": "ZGF0YQ==", "format": "mp3"},
            },
            {
                "type": "file",
                "file": {
                    "file_data": "data:application/pdf;base64,JVBERi0xLjc=",
                    "filename": "report.pdf",
                },
            },
        ],
    }


def test_chat_completions_reject_document_url() -> None:
    target = ModelTarget(
        ref="openai/gpt-5",
        provider="openai",
        name="gpt-5",
        model="gpt-5",
        adapter="chat_completions",
    )

    with pytest.raises(
        ToolangError,
        match="does not accept a URL",
    ):
        chat_completions_models.encode_message(
            target,
            Message(
                role="user",
                parts=(DocumentPart(url="https://example.com/report.pdf"),),
            ),
        )


def test_chat_completions_audio_response_keeps_transcript_on_audio_part() -> None:
    result = chat_completions_models.parse_chat_completion(
        SimpleNamespace(
            choices=(
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        audio=SimpleNamespace(
                            data="ZGF0YQ==",
                            transcript="hello",
                        ),
                        tool_calls=(),
                    )
                ),
            ),
            usage=None,
        ),
        audio_format="mp3",
    )

    assert result.message == Message(
        role="assistant",
        parts=(
            AudioPart(
                data="ZGF0YQ==",
                format="mp3",
                transcript="hello",
            ),
        ),
    )


def test_chat_completions_replays_assistant_multimodal_output_as_text() -> None:
    encoded = chat_completions_models.encode_message(
        ModelTarget(
            ref="openai/gpt-audio",
            provider="openai",
            name="gpt-audio",
            model="gpt-audio",
            adapter="chat_completions",
        ),
        Message(
            role="assistant",
            parts=(
                ImagePart(
                    image_url="data:image/png;base64,aW1hZ2U=",
                    filename="chart.png",
                ),
                AudioPart(
                    data="ZGF0YQ==",
                    format="mp3",
                    transcript="spoken result",
                ),
                DocumentPart(file_id="file-1", filename="report.pdf"),
            ),
        ),
    )

    assert encoded == {
        "role": "assistant",
        "content": "[image:chart.png]\nspoken result\n[document:report.pdf]",
    }


def test_chat_completions_audio_stream_does_not_open_duplicate_text_part(
    monkeypatch,
) -> None:
    events: list[object] = []

    async def record_event(event: object) -> None:
        events.append(event)

    class _Stream:
        async def __aiter__(self):
            yield SimpleNamespace(
                choices=(
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            reasoning_content=None,
                            content="hello",
                            audio=SimpleNamespace(
                                data="ZGF0YQ==",
                                transcript="hello",
                            ),
                            tool_calls=(),
                        )
                    ),
                ),
                usage=None,
            )

        async def close(self) -> None:
            return None

    class _Completions:
        async def create(self, **payload):
            del payload
            return _Stream()

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(
        chat_completions_models, "create_client", lambda target: _Client()
    )

    result = asyncio.run(
        chat_completions_models.stream_chat_completion(
            ModelTarget(
                ref="openai/gpt-audio",
                provider="openai",
                name="gpt-audio",
                model="gpt-audio",
                adapter="chat_completions",
                options={
                    "modalities": ["text", "audio"],
                    "audio": {"format": "mp3", "voice": "alloy"},
                },
            ),
            ModelCall(instructions="", messages=[Message.user("hello")]),
            on_event=record_event,
        )
    )

    assert result.message == Message(
        role="assistant",
        parts=(
            AudioPart(
                data="ZGF0YQ==",
                format="mp3",
                transcript="hello",
            ),
        ),
    )
    assert [
        (type(event).__name__, getattr(event, "kind", None)) for event in events
    ] == [
        ("ModelPartStart", "audio"),
        ("ModelPartEnd", None),
    ]


def test_responses_encode_message_preserves_structured_content() -> None:
    encoded = encode_message(
        Message(role="user", parts=(Message.user("hello").parts[0],))
    )

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
                DocumentPart(
                    url="https://example.com/report.pdf",
                    filename="report.pdf",
                ),
            ),
        )
    )

    assert encoded == {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "describe this"},
            {
                "type": "input_image",
                "image_url": "https://example.com/image.png",
                "detail": "high",
            },
            {
                "type": "input_audio",
                "input_audio": {"data": "ZGF0YQ==", "format": "mp3"},
            },
            {
                "type": "input_file",
                "file_url": "https://example.com/report.pdf",
                "filename": "report.pdf",
            },
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


def test_document_part_preserves_data_url() -> None:
    part = Message.from_data(
        {
            "role": "user",
            "parts": [
                {
                    "type": "document",
                    "data": "data:application/pdf;base64,JVBERi0xLjc=",
                    "filename": "report.pdf",
                }
            ],
        }
    ).parts[0]

    assert isinstance(part, DocumentPart)
    assert part.data == "data:application/pdf;base64,JVBERi0xLjc="
    assert part.media_type == "application/pdf"
    assert part.filename == "report.pdf"


def test_responses_audio_response_keeps_transcript_on_audio_part() -> None:
    result = responses_models.assistant_message(
        SimpleNamespace(
            output=(
                SimpleNamespace(
                    type="message",
                    content=(
                        SimpleNamespace(
                            type="output_audio",
                            data="ZGF0YQ==",
                            format="wav",
                            transcript="hello",
                        ),
                    ),
                ),
            ),
            output_text="hello",
        ),
        tool_calls=(),
    )

    assert result == Message(
        role="assistant",
        parts=(
            AudioPart(
                data="ZGF0YQ==",
                format="wav",
                transcript="hello",
            ),
        ),
    )


def test_responses_image_generation_output_becomes_image_part() -> None:
    result = responses_models.assistant_message(
        SimpleNamespace(
            output=(
                SimpleNamespace(
                    type="image_generation_call",
                    result="aW1hZ2U=",
                ),
            ),
            output_text="",
        ),
        tool_calls=(),
    )

    assert result == Message(
        role="assistant",
        parts=(
            ImagePart(
                image_url="data:image/png;base64,aW1hZ2U=",
                media_type="image/png",
            ),
        ),
    )


def test_agic_preserves_multimodal_steer_and_model_output() -> None:
    image = ImagePart(file_id="image-1")
    audio = AudioPart(
        data="ZGF0YQ==",
        format="wav",
        transcript="done",
    )
    steer = Message(
        role="user",
        parts=(Message.user("inspect").parts[0], image),
    )
    provider = _FakeModels(
        name="openai",
        responses=[
            ModelCallResult(
                message=Message(role="assistant", parts=(audio,)),
            ),
        ],
    )
    prepared = _prepared_agic(
        provider,
        ModelTarget(
            ref="openai/gpt-audio",
            provider="openai",
            name="gpt-audio",
            model="gpt-audio",
            adapter="chat_completions",
        ),
    )
    pending = [
        ControlRecord(
            target="run_1",
            index=1,
            kind="steer",
            timing="next_call",
            payload=SteerControlPayload(
                (Local.typed("Part[]", tuple(steer.parts), "_", 0),)
            ),
        )
    ]
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    def pending_inputs() -> tuple[ControlRecord, ...]:
        current = tuple(pending)
        pending.clear()
        return current

    result = asyncio.run(
        _execute(
            _AgicState(
                prepared=prepared,
                layout=AgentLayout.resident(Path("/tmp"), "home"),
                emit=emit,
                pending_inputs=pending_inputs,
                steer_before_next_step=lambda: False,
                immediate_steer=lambda: False,
                before_call=lambda: None,
                messages=list(prepared.messages),
            )
        )
    )

    assert result == Message(role="assistant", parts=(audio,))
    assert provider.requests[0].messages[-1] == steer
    step_end = next(event for event in events if isinstance(event, StepEnd))
    assert step_end.output == Local.typed("Part[]", (audio,), "_")
    assert [event.type for event in events] == [
        "step_begin",
        "part_begin",
        "part_end",
        "step_end",
    ]


def test_agic_commits_steer_messages_after_step_begin() -> None:
    steer = Message.user("inspect")
    provider = _FakeModels(
        name="openai",
        responses=[ModelCallResult(message=Message.assistant("unused"))],
    )
    prepared = _prepared_agic(
        provider,
        ModelTarget(
            ref="openai/gpt-test",
            provider="openai",
            name="gpt-test",
            model="gpt-test",
            adapter="chat_completions",
        ),
    )
    control = ControlRecord(
        target="run_1",
        index=1,
        kind="steer",
        timing="next_call",
        payload=SteerControlPayload(
            (Local.typed("Part[]", tuple(steer.parts), "_", 0),)
        ),
    )
    original_messages = list(prepared.messages)

    async def emit(_event: RunEvent) -> None:
        assert state.messages == original_messages
        raise RuntimeError("step begin persistence failed")

    state = _AgicState(
        prepared=prepared,
        layout=AgentLayout.resident(Path("/tmp"), "home"),
        emit=emit,
        pending_inputs=lambda: (control,),
        steer_before_next_step=lambda: False,
        immediate_steer=lambda: False,
        before_call=lambda: None,
        messages=list(original_messages),
    )

    with pytest.raises(RuntimeError, match="step begin persistence failed"):
        asyncio.run(_execute(state))

    assert state.messages == original_messages
    assert provider.requests == []


def test_responses_replays_assistant_multimodal_output_as_text() -> None:
    assert encode_message(
        Message(
            role="assistant",
            parts=(
                ImagePart(
                    image_url="data:image/png;base64,aW1hZ2U=",
                    filename="chart.png",
                ),
                AudioPart(
                    data="ZGF0YQ==",
                    format="wav",
                    transcript="spoken result",
                ),
                DocumentPart(file_id="file-1", filename="report.pdf"),
            ),
        )
    ) == {
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": "[image:chart.png]"},
            {"type": "output_text", "text": "spoken result"},
            {"type": "output_text", "text": "[document:report.pdf]"},
        ],
        "id": "msg_current",
        "status": "completed",
    }


def test_responses_non_audio_model_accepts_assistant_audio_history(
    monkeypatch,
) -> None:
    async def fake_invoke_response(target, request, *, stateful):
        del target, request, stateful
        return ModelCallResult(message=Message.assistant("done"))

    monkeypatch.setattr(
        responses_models,
        "invoke_response",
        fake_invoke_response,
    )
    adapter = responses_models.create_model_adapter({})
    result = asyncio.run(
        adapter.invoke(
            ModelTarget(
                ref="openai/gpt-5",
                provider="openai",
                name="gpt-5",
                model="gpt-5",
                adapter="responses",
            ),
            ModelCall(
                instructions="",
                messages=[
                    Message(
                        role="assistant",
                        parts=(
                            AudioPart(
                                data="ZGF0YQ==",
                                format="wav",
                                transcript="previous",
                            ),
                        ),
                    ),
                    Message.user("continue"),
                ],
            ),
        )
    )

    assert result.message == Message.assistant("done")


def test_responses_audio_stream_does_not_open_duplicate_text_part(
    monkeypatch,
) -> None:
    events: list[object] = []

    async def record_event(event: object) -> None:
        events.append(event)

    response = SimpleNamespace(
        id="resp-1",
        output=(
            SimpleNamespace(
                type="message",
                content=(
                    SimpleNamespace(
                        type="output_audio",
                        data="ZGF0YQ==",
                        format="wav",
                        transcript="hello",
                    ),
                ),
            ),
        ),
        output_text="hello",
        usage=None,
    )

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            del exc_type, exc, traceback

        async def __aiter__(self):
            yield SimpleNamespace(
                type="response.output_text.delta",
                delta="hello",
            )

        async def get_final_response(self):
            return response

    class _Responses:
        def stream(self, **payload):
            del payload
            return _Stream()

    class _Client:
        responses = _Responses()

    monkeypatch.setattr(responses_models, "create_client", lambda target: _Client())

    result = asyncio.run(
        responses_models.stream_response(
            ModelTarget(
                ref="openai/gpt-audio",
                provider="openai",
                name="gpt-audio",
                model="gpt-audio",
                adapter="responses",
            ),
            ModelCall(instructions="", messages=[Message.user("hello")]),
            stateful=True,
            on_event=record_event,
        )
    )

    assert result.message == Message(
        role="assistant",
        parts=(
            AudioPart(
                data="ZGF0YQ==",
                format="wav",
                transcript="hello",
            ),
        ),
    )
    assert [
        (type(event).__name__, getattr(event, "kind", None)) for event in events
    ] == [
        ("ModelPartStart", "audio"),
        ("ModelPartEnd", None),
    ]


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
            continuation={"previous_response_id": "resp_1", "baseline_count": 2},
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


def _prepared_agic(
    provider: _FakeModels,
    model: ModelTarget,
) -> _AgicFrame:
    tool = _FakeTool()
    state = SimpleNamespace(
        program=Program(span=Span(1)),
        fingerprint="live-1",
    )
    from toolang.execution.runnables import AgicRoutes

    return _AgicFrame(
        run=BoundRun(
            run_id="run_1",
            root_run_id="run_1",
            thread="thread-1",
            bindings=RunBindings(runnable="agic:main"),
            input=RunnableInput(),
            control_locals=(),
            state=cast(Any, state),
            state_ref=ControlRef("run_1", 0),
            setup=AgentSetup(
                layout=AgentLayout.resident(Path("/"), "alice"),
                providers={},
                adapters={},
                models=(),
                tools={},
                envs={},
            ),
            created_at="2026-04-10T00:00:00Z",
        ),
        agic=AgicDecl(
            name="main",
            input=Parameter(name="_", span=Span(1)),
            messages=(
                AstMessage(
                    role="user",
                    content="Reply directly.",
                    explicit=False,
                    span=Span(1),
                ),
            ),
            span=Span(1),
        ),
        model=model,
        adapter=provider,
        instructions="",
        prompt_context="",
        messages=(Message.user("hello"),),
        tools={tool.name: tool},
        runtime_tools={},
        routes=AgicRoutes(),
        services=(),
    )


def _run_agic(prepared: _AgicFrame) -> Message | None:
    return asyncio.run(
        _execute(
            _AgicState(
                prepared=prepared,
                layout=AgentLayout.resident(Path("/tmp"), "home"),
                emit=_ignore_event,
                pending_inputs=tuple,
                steer_before_next_step=lambda: False,
                immediate_steer=lambda: False,
                before_call=lambda: None,
                messages=list(prepared.messages),
            )
        )
    )
