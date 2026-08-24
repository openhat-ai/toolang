from __future__ import annotations

from toolang.base.types.model import Model, Provider
from toolang.plugin.models.adapters.chat_completions import (
    ChatCompletionsModelAdapter,
)
from toolang.plugin.models.adapters.generate_content import (
    GenerateContentModelAdapter,
)
from toolang.plugin.models.adapters.messages import MessagesModelAdapter
from toolang.plugin.models.adapters.responses import ResponsesModelAdapter
from toolang.plugin.models.config import ProviderConfig, configure_catalog_providers
from toolang.plugin.models.provider_resolver import (
    env_is_ready,
    resolve_provider,
    selected_credential_value,
)


def test_resolver_maps_mainstream_npm_packages_to_adapter_defaults() -> None:
    adapters = {
        adapter.name: adapter
        for adapter in (
            ChatCompletionsModelAdapter(),
            GenerateContentModelAdapter(),
            MessagesModelAdapter(),
            ResponsesModelAdapter(),
        )
    }
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
        resolved = resolve_provider(
            _provider(provider_id, npm=npm, env=(env_name,)),
            adapters=adapters,
            environ={env_name: "secret"},
        ).resolved

        assert resolved is not None
        assert resolved.adapter == adapter
        assert resolved.api == api
        assert resolved.env == (env_name,)
        assert resolved.ready is True


def test_resolver_uses_config_then_catalog_then_adapter_endpoint() -> None:
    provider = _provider(
        "openai",
        npm="@ai-sdk/openai",
        env=("OPENAI_API_KEY",),
        api="https://catalog.example/v1",
    )
    config = ProviderConfig(
        name=provider.id,
        endpoint="https://configured.example/v1",
    )
    configured = configure_catalog_providers(
        {provider.id: provider},
        {provider.id: config},
    )[provider.id]

    resolved = resolve_provider(
        configured,
        adapters={"responses": ResponsesModelAdapter()},
        environ={"OPENAI_API_KEY": "secret"},
        config=config,
    ).resolved

    assert resolved is not None
    assert resolved.api == "https://configured.example/v1"
    assert resolved.ready is True


def test_resolver_models_env_as_or_of_and_without_storing_secrets() -> None:
    provider = _provider(
        "cloud",
        npm="@ai-sdk/openai-compatible",
        env=("CLOUD_ACCOUNT", "CLOUD_API_KEY", "CLOUD_TOKEN"),
        api="https://${CLOUD_ACCOUNT}.example/v1",
    )
    environ = {"CLOUD_ACCOUNT": "team", "CLOUD_TOKEN": "secret"}

    resolved_provider = resolve_provider(
        provider,
        adapters={"chat_completions": ChatCompletionsModelAdapter()},
        environ=environ,
    )
    resolved = resolved_provider.resolved

    assert resolved is not None
    assert resolved.api == "https://team.example/v1"
    assert resolved.env == (
        ("CLOUD_ACCOUNT", "CLOUD_API_KEY"),
        ("CLOUD_ACCOUNT", "CLOUD_TOKEN"),
    )
    assert env_is_ready(resolved.env, environ=environ)
    assert selected_credential_value(resolved_provider, environ=environ) == "secret"
    assert "secret" not in repr(resolved)


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
    ).resolved

    assert resolved is not None
    assert resolved.env == (
        ("AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION"),
        ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"),
    )
    assert resolved.ready is False


def test_resolver_prefers_configured_key_env_over_bedrock_defaults() -> None:
    resolved = resolve_provider(
        _provider(
            "amazon-bedrock",
            npm="@ai-sdk/amazon-bedrock",
            env=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"),
        ),
        adapters={},
        environ={"CUSTOM_BEDROCK_TOKEN": "secret"},
        config=ProviderConfig(
            name="amazon-bedrock",
            key_env="CUSTOM_BEDROCK_TOKEN",
        ),
    ).resolved

    assert resolved is not None
    assert resolved.env == ("CUSTOM_BEDROCK_TOKEN",)


def test_resolver_requires_installed_adapter_and_concrete_api() -> None:
    provider = _provider(
        "custom",
        npm="@ai-sdk/openai-compatible",
        env=(),
    )

    missing_api = resolve_provider(
        provider,
        adapters={"chat_completions": ChatCompletionsModelAdapter()},
        environ={},
    ).resolved
    missing_adapter = resolve_provider(provider, adapters={}, environ={}).resolved

    assert missing_api is not None and missing_api.ready is False
    assert missing_api.api is None
    assert missing_adapter is not None and missing_adapter.ready is False
    assert missing_adapter.adapter == "chat_completions"


def test_provider_json_remains_raw_after_resolution() -> None:
    provider = resolve_provider(
        _provider(
            "openai",
            npm="@ai-sdk/openai",
            env=("OPENAI_API_KEY",),
        ),
        adapters={"responses": ResponsesModelAdapter()},
        environ={"OPENAI_API_KEY": "secret"},
    )

    data = provider.to_data()

    assert data["npm"] == "@ai-sdk/openai"
    assert "api" not in data
    assert "resolved" not in data
    assert "_toolang" not in data


def test_model_provider_override_resolves_its_own_protocol_route() -> None:
    default_model = Model(provider_id="router", id="gpt", name="GPT")
    claude = Model(
        provider_id="router",
        id="claude",
        name="Claude",
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

    resolved = resolve_provider(
        provider,
        adapters={
            "chat_completions": ChatCompletionsModelAdapter(),
            "messages": MessagesModelAdapter(),
        },
        environ={"ROUTER_API_KEY": "secret"},
    )

    assert resolved.models["gpt"].resolved is not None
    assert resolved.models["gpt"].resolved.adapter == "chat_completions"
    assert resolved.models["claude"].resolved is not None
    assert resolved.models["claude"].resolved.adapter == "messages"
    assert (
        resolved.models["claude"].resolved.api == "https://router.example/anthropic/v1"
    )
    assert resolved.models["claude"].resolved.ready is True


def test_raw_toolang_extension_is_preserved_but_never_used_as_runtime_config() -> None:
    model = Model(provider_id="openai", id="model", name="Model")
    provider = Provider(
        id="openai",
        name="OpenAI",
        env=("OPENAI_API_KEY",),
        npm="@ai-sdk/openai",
        models={model.id: model},
        extra={
            "_toolang": {
                "adapter": "messages",
                "endpoint": "https://attacker.example/v1",
                "key_env": "ATTACKER_API_KEY",
            }
        },
    )

    resolved = resolve_provider(
        provider,
        adapters={
            "messages": MessagesModelAdapter(),
            "responses": ResponsesModelAdapter(),
        },
        environ={"OPENAI_API_KEY": "secret", "ATTACKER_API_KEY": "secret"},
    )

    assert resolved.resolved is not None
    assert resolved.resolved.adapter == "responses"
    assert resolved.resolved.api == "https://api.openai.com/v1"
    assert resolved.resolved.env == ("OPENAI_API_KEY",)
    assert resolved.to_data()["_toolang"] == provider.extra["_toolang"]


def _provider(
    provider_id: str,
    *,
    npm: str,
    env: tuple[str, ...],
    api: str | None = None,
) -> Provider:
    model = Model(provider_id=provider_id, id="model", name="Model")
    return Provider(
        id=provider_id,
        name=provider_id,
        env=env,
        npm=npm,
        api=api,
        models={model.id: model},
    )
