from __future__ import annotations

from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.model import Model, ModelToolang, Provider, ProviderToolang
from toolang.plugin.adapters.chat_completions import (
    ChatCompletionsModelAdapter,
)
from toolang.plugin.adapters.generate_content import (
    GenerateContentModelAdapter,
)
from toolang.plugin.adapters.messages import MessagesModelAdapter
from toolang.plugin.adapters.responses import ResponsesModelAdapter
from toolang.plugin.models.provider_resolver import (
    env_is_ready,
    model_adapter,
    model_api,
    resolve_provider,
    selected_credential_value,
)


def _adapters() -> dict[str, ModelAdapter]:
    return {
        adapter.name: adapter
        for adapter in (
            ChatCompletionsModelAdapter(),
            GenerateContentModelAdapter(),
            MessagesModelAdapter(),
            ResponsesModelAdapter(),
        )
    }


def test_resolver_maps_mainstream_npm_packages_to_adapter_defaults() -> None:
    adapters = _adapters()
    cases = (
        (
            "anthropic",
            "@ai-sdk/anthropic",
            "ANTHROPIC_API_KEY",
            "messages",
            "https://api.anthropic.com/v1",
        ),
        (
            "google",
            "@ai-sdk/google",
            "GEMINI_API_KEY",
            "generate_content",
            "https://generativelanguage.googleapis.com/v1beta",
        ),
        (
            "openai",
            "@ai-sdk/openai",
            "OPENAI_API_KEY",
            "responses",
            "https://api.openai.com/v1",
        ),
    )

    for provider_id, npm, env_name, adapter, api in cases:
        environ = {env_name: "secret"}
        resolved = resolve_provider(
            _provider(provider_id, npm=npm, env=(env_name,)),
            adapters=adapters,
            environ=environ,
        )
        model = resolved.models["model"]

        assert resolved._toolang.adapter == adapter
        assert resolved._toolang.env == (env_name,)
        assert model_api(resolved, model, adapters=adapters, environ=environ) == api
        assert model._toolang.ready is True


def test_resolver_prefers_the_catalog_api_over_the_adapter_default() -> None:
    provider = _provider(
        "openai",
        npm="@ai-sdk/openai",
        env=("OPENAI_API_KEY",),
        api="https://catalog.example/v1",
    )
    adapters = {"responses": ResponsesModelAdapter()}
    environ = {"OPENAI_API_KEY": "secret"}

    resolved = resolve_provider(provider, adapters=adapters, environ=environ)
    model = resolved.models["model"]

    assert (
        model_api(resolved, model, adapters=adapters, environ=environ)
        == "https://catalog.example/v1"
    )
    assert model._toolang.ready is True


def test_resolver_models_env_as_or_of_and_without_storing_secrets() -> None:
    provider = _provider(
        "cloud",
        npm="@ai-sdk/openai-compatible",
        env=("CLOUD_API_KEY", "CLOUD_TOKEN"),
        api="https://${CLOUD_ACCOUNT}.example/v1",
    )
    environ = {"CLOUD_ACCOUNT": "team", "CLOUD_TOKEN": "secret"}
    adapters = {"chat_completions": ChatCompletionsModelAdapter()}

    resolved_provider = resolve_provider(
        provider,
        adapters=adapters,
        environ=environ,
    )
    model = resolved_provider.models["model"]
    resolved = resolved_provider._toolang.env

    assert (
        model_api(resolved_provider, model, adapters=adapters, environ=environ)
        == "https://team.example/v1"
    )
    assert resolved == ("CLOUD_API_KEY", "CLOUD_TOKEN")
    assert env_is_ready(resolved, environ=environ)
    assert selected_credential_value(resolved_provider, environ=environ) == "secret"
    assert "secret" not in repr(resolved_provider)


def test_resolver_applies_explicit_bedrock_env_alternatives() -> None:
    resolved = resolve_provider(
        _provider(
            "amazon-bedrock",
            npm="@ai-sdk/amazon-bedrock",
            env=(
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_REGION",
                "AWS_BEARER_TOKEN_BEDROCK",
            ),
        ),
        adapters={},
        environ={},
    )

    assert resolved._toolang.env == (
        ("AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION"),
        ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"),
    )
    assert resolved.models["model"]._toolang.ready is False


def test_resolver_requires_installed_adapter_and_concrete_api() -> None:
    provider = _provider("custom", npm="@ai-sdk/openai-compatible", env=())
    adapters = {"chat_completions": ChatCompletionsModelAdapter()}

    missing_api = resolve_provider(provider, adapters=adapters, environ={})
    missing_adapter = resolve_provider(provider, adapters={}, environ={})

    api_model = missing_api.models["model"]
    assert api_model._toolang.ready is False
    assert model_api(missing_api, api_model, adapters=adapters, environ={}) is None

    assert missing_adapter._toolang.adapter == "chat_completions"
    assert missing_adapter.models["model"]._toolang.ready is False


def test_provider_json_remains_raw_after_resolution() -> None:
    provider = resolve_provider(
        _provider("openai", npm="@ai-sdk/openai", env=("OPENAI_API_KEY",)),
        adapters={"responses": ResponsesModelAdapter()},
        environ={"OPENAI_API_KEY": "secret"},
    )

    data = provider.to_data()

    assert data["npm"] == "@ai-sdk/openai"
    assert "api" not in data
    assert "_toolang" not in data


def test_model_provider_override_resolves_its_own_protocol_route() -> None:
    default_model = _model("router", "gpt", "GPT")
    claude = Model(
        id="claude",
        name="Claude",
        _toolang=ModelToolang(provider="router"),
        provider={
            "npm": "@ai-sdk/anthropic",
            "api": "https://router.example/anthropic/v1",
        },
    )
    provider = Provider(
        id="router",
        name="Router",
        env=("ROUTER_API_KEY",),
        npm="@ai-sdk/openai-compatible",
        api="https://router.example/openai/v1",
        models={"gpt": default_model, "claude": claude},
    )
    adapters = {
        "chat_completions": ChatCompletionsModelAdapter(),
        "messages": MessagesModelAdapter(),
    }
    environ = {"ROUTER_API_KEY": "secret"}

    resolved = resolve_provider(provider, adapters=adapters, environ=environ)
    gpt = resolved.models["gpt"]
    claude_resolved = resolved.models["claude"]

    assert model_adapter(resolved, gpt) == "chat_completions"
    assert model_adapter(resolved, claude_resolved) == "messages"
    assert (
        model_api(resolved, claude_resolved, adapters=adapters, environ=environ)
        == "https://router.example/anthropic/v1"
    )
    assert claude_resolved._toolang.ready is True


def test_resolver_reuses_frozen_model_catalog_fields() -> None:
    model = Model(
        id="model",
        name="Model",
        _toolang=ModelToolang(provider="openai"),
        modalities={"input": ("text",)},
        cost={"input": 1},
    )
    provider = Provider(
        id="openai",
        name="OpenAI",
        env=("OPENAI_API_KEY",),
        npm="@ai-sdk/openai",
        models={model.id: model},
    )

    resolved = resolve_provider(
        provider,
        adapters={"responses": ResponsesModelAdapter()},
        environ={"OPENAI_API_KEY": "secret"},
    ).models[model.id]

    assert resolved is not model
    assert resolved.modalities is model.modalities
    assert resolved.cost is model.cost


def test_raw_toolang_extension_is_ignored_as_runtime_config() -> None:
    model = Model(
        id="model",
        name="Model",
        _toolang=ModelToolang(provider="openai"),
        provider={
            "_toolang": {
                "adapter": "messages",
                "endpoint": "https://attacker.example/v1",
            }
        },
    )
    provider = Provider(
        id="openai",
        name="OpenAI",
        env=("OPENAI_API_KEY",),
        npm="@ai-sdk/openai",
        models={model.id: model},
    )
    adapters = {
        "messages": MessagesModelAdapter(),
        "responses": ResponsesModelAdapter(),
    }
    environ = {"OPENAI_API_KEY": "secret", "ATTACKER_API_KEY": "secret"}

    resolved = resolve_provider(provider, adapters=adapters, environ=environ)
    resolved_model = resolved.models["model"]

    assert model_adapter(resolved, resolved_model) == "responses"
    assert (
        model_api(resolved, resolved_model, adapters=adapters, environ=environ)
        == "https://api.openai.com/v1"
    )
    assert resolved._toolang.env == ("OPENAI_API_KEY",)


def test_resolver_uses_a_declared_adapter_instead_of_an_npm_package() -> None:
    adapters = {
        "chat_completions": ChatCompletionsModelAdapter(),
        "responses": ResponsesModelAdapter(),
    }
    declared = Provider(
        id="local",
        name="Local",
        env=(),
        models={},
        _toolang=ProviderToolang(adapter="chat_completions"),
        api="http://local.test/v1",
    )
    both = Provider(
        id="both",
        name="Both",
        env=(),
        npm="@ai-sdk/openai",
        _toolang=ProviderToolang(adapter="chat_completions"),
        api="http://both.test/v1",
        models={},
    )

    resolved = resolve_provider(declared, adapters=adapters, environ={})
    preferred = resolve_provider(both, adapters=adapters, environ={})

    assert resolved._toolang.adapter == "chat_completions"
    assert "npm" not in resolved.to_data()
    assert preferred._toolang.adapter == "chat_completions"
    assert preferred.to_data()["npm"] == "@ai-sdk/openai"


def _provider(
    provider_id: str,
    *,
    npm: str,
    env: tuple[str, ...],
    api: str | None = None,
) -> Provider:
    model = _model(provider_id, "model", "Model")
    return Provider(
        id=provider_id,
        name=provider_id,
        env=env,
        npm=npm,
        api=api,
        models={model.id: model},
    )


def _model(provider_id: str, model_id: str, name: str) -> Model:
    return Model(
        id=model_id,
        name=name,
        _toolang=ModelToolang(provider=provider_id),
    )
