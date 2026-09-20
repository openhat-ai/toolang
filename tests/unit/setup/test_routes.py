from __future__ import annotations

from dataclasses import replace

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
from toolang.plugin.adapters._credentials import credential_value
from toolang.setup.routes import (
    env_is_ready,
    model_adapter,
    resolve_provider,
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

        assert resolved._toolang.route.adapter == adapter
        assert resolved._toolang.route.env == (env_name,)
        assert model._toolang.route.api == api
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

    assert model._toolang.route.api == "https://catalog.example/v1"
    assert model._toolang.ready is True


def test_resolver_models_env_as_or_of_and_without_storing_secrets() -> None:
    provider = _provider(
        "cloud",
        npm="@ai-sdk/openai-compatible",
        env=("CLOUD_ACCOUNT", "CLOUD_API_KEY", "CLOUD_TOKEN"),
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
    resolved = resolved_provider._toolang.route.env

    assert model._toolang.route.api == "https://team.example/v1"
    assert resolved == (
        ("CLOUD_ACCOUNT", "CLOUD_API_KEY"),
        ("CLOUD_ACCOUNT", "CLOUD_TOKEN"),
    )
    assert env_is_ready(resolved, environ=environ)
    assert (
        credential_value(resolved_provider._toolang.route.env, environ=environ)
        == "secret"
    )
    assert credential_value(resolved, environ=environ) == "secret"
    assert "secret" not in repr(resolved_provider)

    missing_key = resolve_provider(
        provider, adapters=adapters, environ={"CLOUD_ACCOUNT": "team"}
    )
    assert missing_key.models["model"]._toolang.ready is False
    assert (
        credential_value(
            missing_key._toolang.route.env, environ={"CLOUD_ACCOUNT": "team"}
        )
        is None
    )


def test_resolver_preserves_explicit_plugin_env_alternatives() -> None:
    provider = replace(
        _provider("cloud", npm="@ai-sdk/openai", env=("IGNORED_API_KEY",)),
        _toolang=ProviderToolang(env=(("ACCOUNT", "TOKEN"), "FALLBACK")),
    )
    resolved = resolve_provider(
        provider, adapters=_adapters(), environ={"FALLBACK": "credential"}
    )

    assert resolved._toolang.route.env == (("ACCOUNT", "TOKEN"), "FALLBACK")
    assert resolved.models["model"]._toolang.ready is True


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
        environ={"AWS_BEARER_TOKEN_BEDROCK": "secret", "AWS_REGION": "region"},
    )

    assert resolved._toolang.route.env == (
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
    assert api_model._toolang.route.api is None

    assert missing_adapter._toolang.route.adapter is None
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
    assert claude_resolved._toolang.route.api == "https://router.example/anthropic/v1"
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
    assert resolved_model._toolang.route.api == "https://api.openai.com/v1"
    assert resolved._toolang.route.env == ("OPENAI_API_KEY",)


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

    assert resolved._toolang.route.adapter == "chat_completions"
    assert "npm" not in resolved.to_data()
    assert preferred._toolang.route.adapter == "chat_completions"
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


def test_imported_catalog_env_requires_account_and_credential():
    from toolang.plugin.catalogs.models_dev.parsing import parse_model_catalog_data

    raw = {
        "cloud": {
            "id": "cloud",
            "name": "Cloud",
            "npm": "@ai-sdk/openai",
            "env": ["CLOUD_ACCOUNT", "CLOUD_API_KEY", "CLOUD_TOKEN"],
            "models": {
                "one": {"id": "one", "name": "One", "modalities": {}, "limit": {}}
            },
        }
    }
    provider = parse_model_catalog_data(raw)["cloud"]
    for environ, ready in (
        ({"CLOUD_ACCOUNT": "account"}, False),
        ({"CLOUD_API_KEY": "key"}, False),
        ({"CLOUD_ACCOUNT": "account", "CLOUD_API_KEY": "key"}, True),
    ):
        resolved = resolve_provider(provider, adapters=_adapters(), environ=environ)
        assert resolved.models["one"]._toolang.ready is ready
        assert credential_value(resolved._toolang.route.env, environ=environ) == (
            "key" if ready else None
        )


def test_routes_publish_independent_failures_and_preserve_declarations():
    provider = _provider(
        "test",
        npm="@ai-sdk/openai",
        env=("ACCOUNT", "TEST_API_KEY"),
        api="https://${ACCOUNT}.example/v1",
    )
    model = replace(provider.models["model"], provider={"shape": "messages"})
    provider = replace(provider, models={"model": model})
    original = provider.to_data()
    for environ, adapters, expected in (
        ({}, {}, (None, None, None)),
        ({"ACCOUNT": "team"}, {}, (None, "https://team.example/v1", None)),
        (
            {"ACCOUNT": "team", "TEST_API_KEY": "secret"},
            {},
            (None, "https://team.example/v1", (("ACCOUNT", "TEST_API_KEY"),)),
        ),
        ({}, _adapters(), ("messages", None, None)),
    ):
        resolved = resolve_provider(provider, adapters=adapters, environ=environ)
        model = resolved.models["model"]
        route = model._toolang.route
        assert (route.adapter, route.api, route.env) == expected
        assert model._toolang.ready is False
        assert resolved.to_data() == original
        assert resolved._toolang.adapter == provider._toolang.adapter
        assert resolved._toolang.env == provider._toolang.env
        assert model.provider is not None and "_toolang" not in model.provider

    ready = resolve_provider(
        provider,
        adapters=_adapters(),
        environ={
            "ACCOUNT": "team",
            "TEST_API_KEY": "secret",
        },
    )
    assert ready.models["model"]._toolang.ready is True
    assert ready._toolang.route.adapter == "responses"
    assert ready.models["model"]._toolang.route.adapter == "messages"


def test_resolver_uses_npm_service_endpoints_for_provider_and_model_routes():
    provider = _provider("gateway", npm="@ai-sdk/groq", env=())
    resolved = resolve_provider(provider, adapters=_adapters(), environ={})
    assert resolved._toolang.route.api == "https://api.groq.com/openai/v1"
    assert resolved.models["model"]._toolang.ready
    assert resolved.models["model"]._toolang.route.api == resolved._toolang.route.api

    model = replace(provider.models["model"], provider={"npm": "@ai-sdk/mistral"})
    provider = replace(provider, models={"model": model})
    resolved = resolve_provider(provider, adapters=_adapters(), environ={})
    assert resolved.models["model"]._toolang.route.api == "https://api.mistral.ai/v1"
    assert resolved.models["model"]._toolang.ready
    explicit = resolve_provider(
        replace(provider, api="https://catalog.test/v1"),
        adapters=_adapters(),
        environ={},
    )
    assert explicit.models["model"]._toolang.route.api == "https://catalog.test/v1"
    model = replace(
        model, provider={"npm": "@ai-sdk/mistral", "api": "https://model.test/v1"}
    )
    explicit = resolve_provider(
        replace(provider, api="https://catalog.test/v1", models={"model": model}),
        adapters=_adapters(),
        environ={},
    )
    assert explicit.models["model"]._toolang.route.api == "https://model.test/v1"


def test_resolver_does_not_apply_npm_endpoints_to_explicit_adapter_declarations():
    provider = _provider("custom", npm="@ai-sdk/groq", env=())
    declared = replace(provider, _toolang=ProviderToolang(adapter="chat_completions"))
    resolved = resolve_provider(declared, adapters=_adapters(), environ={})
    assert resolved._toolang.route.api is None
    assert resolved.models["model"]._toolang.route.api is None
    for declaration in (
        {"shape": "chat_completions"},
        {"_toolang": ProviderToolang(adapter="chat_completions")},
    ):
        model = replace(
            provider.models["model"], provider={"npm": "@ai-sdk/mistral", **declaration}
        )
        resolved = resolve_provider(
            replace(provider, models={"model": model}), adapters=_adapters(), environ={}
        )
        assert resolved.models["model"]._toolang.route.api is None
