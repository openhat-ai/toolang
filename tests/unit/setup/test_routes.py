from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from toolang.base.types.model import ModelCatalogSnapshot

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
    resolve_catalog_providers,
)

from toolang.base.types.model import ModelProvider


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
        resolved = resolve_catalog_providers(
            _provider(provider_id, npm=npm, env=(env_name,)),
            adapters=adapters,
            environ=environ,
        )
        model = _model_for(resolved, "model")

        assert _provider_for(resolved)._toolang.route.adapter == adapter
        assert _provider_for(resolved)._toolang.route.env == (env_name,)
        assert model._toolang.route.api == api
        assert model._toolang.ready is True


@pytest.mark.parametrize("provider_id", ["openrouter", "vercel"])
@pytest.mark.parametrize(
    ("model_id", "expected_adapter"),
    [
        ("anthropic/claude-sonnet", "messages"),
        ("openai/gpt-6-luna", "responses"),
        ("google/gemini-pro", "chat_completions"),
        ("unrecognized/model", "chat_completions"),
        ("anthropic", "chat_completions"),
        ("anthropic/", "chat_completions"),
    ],
)
def test_gateway_model_namespace_selects_native_adapter(
    provider_id, model_id, expected_adapter
):
    provider = _provider(
        provider_id,
        npm=(
            "@openrouter/ai-sdk-provider"
            if provider_id == "openrouter"
            else "@ai-sdk/gateway"
        ),
        env=(
            ("OPENROUTER_API_KEY",)
            if provider_id == "openrouter"
            else ("AI_GATEWAY_API_KEY",)
        ),
    )
    model = replace(_model_for(provider, "model"), id=model_id)
    snapshot = _replace_catalog(provider, models={model_id: model})
    resolved = resolve_catalog_providers(
        snapshot,
        adapters=_adapters(),
        environ={"OPENROUTER_API_KEY": "secret", "AI_GATEWAY_API_KEY": "secret"},
    )

    resolved_model = _model_for(resolved, model_id)
    assert resolved_model._toolang.route.adapter == expected_adapter
    if expected_adapter in {"messages", "responses"}:
        expected_api = (
            "https://openrouter.ai/api/v1"
            if provider_id == "openrouter"
            else "https://ai-gateway.vercel.sh/v1"
        )
        assert resolved_model._toolang.route.api == expected_api


@pytest.mark.parametrize(
    "declaration",
    [
        ModelProvider(npm="@ai-sdk/openai-compatible"),
        ModelProvider(shape="chat_completions"),
        ModelProvider(_toolang=ProviderToolang(adapter="chat_completions")),
    ],
)
def test_gateway_explicit_model_adapter_overrides_namespace_inference(declaration):
    provider = _provider("vercel", npm="@ai-sdk/gateway", env=("AI_GATEWAY_API_KEY",))
    model = replace(
        _model_for(provider, "model"),
        id="anthropic/claude-sonnet",
        provider=declaration,
    )
    resolved = resolve_catalog_providers(
        _replace_catalog(provider, models={model.id: model}),
        adapters=_adapters(),
        environ={"AI_GATEWAY_API_KEY": "secret"},
    )

    resolved_model = _model_for(resolved, model.id)
    assert resolved_model._toolang.route.adapter == "chat_completions"
    if declaration.shape is not None or declaration.npm is not None:
        assert resolved_model._toolang.route.api is None


def test_gateway_model_explicit_npm_keeps_its_own_adapter_api():
    provider = _provider("vercel", npm="@ai-sdk/gateway", env=("AI_GATEWAY_API_KEY",))
    model = replace(
        _model_for(provider, "model"),
        id="anthropic/claude-sonnet",
        provider=ModelProvider(npm="@ai-sdk/anthropic"),
    )
    resolved = resolve_catalog_providers(
        _replace_catalog(provider, models={model.id: model}),
        adapters=_adapters(),
        environ={"AI_GATEWAY_API_KEY": "secret"},
    )

    route = _model_for(resolved, model.id)._toolang.route
    assert route.adapter == "messages"
    assert route.api == "https://api.anthropic.com/v1"


def test_trusted_gateway_provider_adapter_overrides_namespace_inference():
    provider = _provider("vercel", npm="@ai-sdk/gateway", env=("AI_GATEWAY_API_KEY",))
    provider = _replace_catalog(
        provider,
        _toolang=ProviderToolang(adapter="chat_completions"),
    )
    model = replace(_model_for(provider, "model"), id="anthropic/claude-sonnet")
    resolved = resolve_catalog_providers(
        _replace_catalog(provider, models={model.id: model}),
        adapters=_adapters(),
        environ={"AI_GATEWAY_API_KEY": "secret"},
    )

    assert _model_for(resolved, model.id)._toolang.route.adapter == "chat_completions"


def test_resolver_prefers_the_catalog_api_over_the_adapter_default() -> None:
    provider = _provider(
        "openai",
        npm="@ai-sdk/openai",
        env=("OPENAI_API_KEY",),
        api="https://catalog.example/v1",
    )
    adapters = {"responses": ResponsesModelAdapter()}
    environ = {"OPENAI_API_KEY": "secret"}

    resolved = resolve_catalog_providers(provider, adapters=adapters, environ=environ)
    model = _model_for(resolved, "model")

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

    resolved_provider = resolve_catalog_providers(
        provider,
        adapters=adapters,
        environ=environ,
    )
    model = _model_for(resolved_provider, "model")
    resolved = _provider_for(resolved_provider)._toolang.route.env

    assert model._toolang.route.api == "https://team.example/v1"
    assert resolved == (
        ("CLOUD_ACCOUNT", "CLOUD_API_KEY"),
        ("CLOUD_ACCOUNT", "CLOUD_TOKEN"),
    )
    assert env_is_ready(resolved, environ=environ)
    assert (
        credential_value(
            _provider_for(resolved_provider)._toolang.route.env, environ=environ
        )
        == "secret"
    )
    assert credential_value(resolved, environ=environ) == "secret"
    assert "secret" not in repr(resolved_provider)

    missing_key = resolve_catalog_providers(
        provider, adapters=adapters, environ={"CLOUD_ACCOUNT": "team"}
    )
    assert _model_for(missing_key, "model")._toolang.ready is False
    assert (
        credential_value(
            _provider_for(missing_key)._toolang.route.env,
            environ={"CLOUD_ACCOUNT": "team"},
        )
        is None
    )


def test_resolver_preserves_explicit_plugin_env_alternatives() -> None:
    provider = _replace_catalog(
        _provider("cloud", npm="@ai-sdk/openai", env=("IGNORED_API_KEY",)),
        _toolang=ProviderToolang(env=(("ACCOUNT", "TOKEN"), "FALLBACK")),
    )
    resolved = resolve_catalog_providers(
        provider, adapters=_adapters(), environ={"FALLBACK": "credential"}
    )

    assert _provider_for(resolved)._toolang.route.env == (
        ("ACCOUNT", "TOKEN"),
        "FALLBACK",
    )
    assert _model_for(resolved, "model")._toolang.ready is True


def test_resolver_applies_explicit_bedrock_env_alternatives() -> None:
    resolved = resolve_catalog_providers(
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

    assert _provider_for(resolved)._toolang.route.env == (
        ("AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION"),
        ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"),
    )
    assert _model_for(resolved, "model")._toolang.ready is False


def test_resolver_requires_installed_adapter_and_concrete_api() -> None:
    provider = _provider("custom", npm="@ai-sdk/openai-compatible", env=())
    adapters = {"chat_completions": ChatCompletionsModelAdapter()}

    missing_api = resolve_catalog_providers(provider, adapters=adapters, environ={})
    missing_adapter = resolve_catalog_providers(provider, adapters={}, environ={})

    api_model = _model_for(missing_api, "model")
    assert api_model._toolang.ready is False
    assert api_model._toolang.route.api is None

    assert _provider_for(missing_adapter)._toolang.route.adapter is None
    assert _model_for(missing_adapter, "model")._toolang.ready is False


def test_vercel_gateway_provider_receives_app_attribution_headers() -> None:
    provider = _provider(
        "vercel",
        npm="@ai-sdk/gateway",
        env=("AI_GATEWAY_API_KEY",),
    )

    resolved = resolve_catalog_providers(
        provider,
        adapters=_adapters(),
        environ={"AI_GATEWAY_API_KEY": "secret"},
    )

    route = _provider_for(resolved)._toolang.route
    model = _model_for(resolved, "model")
    assert route.api == "https://ai-gateway.vercel.sh/v1"
    assert route.headers == {
        "http-referer": "https://toolang.ai",
        "x-title": "Toolang",
    }
    assert model._toolang.route.headers == route.headers


def test_vercel_gateway_attribution_is_not_applied_to_other_providers() -> None:
    regular_provider = _provider(
        "custom",
        npm="@ai-sdk/gateway",
        env=("CUSTOM_API_KEY",),
        api="https://custom.example/v1",
    )
    openrouter_provider = _provider(
        "openrouter",
        npm="@openrouter/ai-sdk-provider",
        env=("OPENROUTER_API_KEY",),
    )

    regular = resolve_catalog_providers(
        regular_provider,
        adapters=_adapters(),
        environ={"CUSTOM_API_KEY": "custom", "OPENROUTER_API_KEY": "router"},
    )
    openrouter = resolve_catalog_providers(
        openrouter_provider,
        adapters=_adapters(),
        environ={"CUSTOM_API_KEY": "custom", "OPENROUTER_API_KEY": "router"},
    )

    assert _provider_for(regular)._toolang.route.api == "https://custom.example/v1"
    assert _provider_for(regular)._toolang.route.headers == {}
    assert _model_for(regular, "model")._toolang.route.headers == {}
    assert _provider_for(openrouter)._toolang.route.headers == {
        "HTTP-Referer": "https://toolang.ai",
        "X-OpenRouter-Title": "Toolang",
        "X-OpenRouter-Categories": "cli-agent,personal-agent",
    }
    assert "x-title" not in _provider_for(openrouter)._toolang.route.headers


def test_explicit_model_header_overrides_gateway_convention_case_insensitively() -> (
    None
):
    gateway = _provider(
        "vercel",
        npm="@ai-sdk/gateway",
        env=("AI_GATEWAY_API_KEY",),
    )
    model = replace(
        _model_for(gateway, "model"),
        provider=ModelProvider(headers={"X-Title": "Custom App"}),
    )
    gateway = _replace_catalog(gateway, models={model.id: model})

    resolved = resolve_catalog_providers(
        gateway,
        adapters=_adapters(),
        environ={"AI_GATEWAY_API_KEY": "secret"},
    )

    assert _model_for(resolved, "model")._toolang.route.headers == {
        "http-referer": "https://toolang.ai",
        "X-Title": "Custom App",
    }


def test_provider_json_remains_raw_after_resolution() -> None:
    provider = resolve_catalog_providers(
        _provider("openai", npm="@ai-sdk/openai", env=("OPENAI_API_KEY",)),
        adapters={"responses": ResponsesModelAdapter()},
        environ={"OPENAI_API_KEY": "secret"},
    )

    data = _provider_data(provider)

    assert data["npm"] == "@ai-sdk/openai"
    assert "api" not in data
    assert "_toolang" not in data


def test_model_provider_override_resolves_its_own_protocol_route() -> None:
    default_model = _model("router", "gpt", "GPT")
    claude = Model(
        id="claude",
        name="Claude",
        _toolang=ModelToolang(provider="router"),
        provider=ModelProvider(
            npm="@ai-sdk/anthropic", api="https://router.example/anthropic/v1"
        ),
    )
    provider = _catalog(
        Provider(
            id="router",
            name="Router",
            env=("ROUTER_API_KEY",),
            npm="@ai-sdk/openai-compatible",
            api="https://router.example/openai/v1",
        ),
        {"gpt": default_model, "claude": claude},
    )
    adapters = {
        "chat_completions": ChatCompletionsModelAdapter(),
        "messages": MessagesModelAdapter(),
    }
    environ = {"ROUTER_API_KEY": "secret"}

    resolved = resolve_catalog_providers(provider, adapters=adapters, environ=environ)
    gpt = _model_for(resolved, "gpt")
    claude_resolved = _model_for(resolved, "claude")

    assert model_adapter(_provider_for(resolved), gpt) == "chat_completions"
    assert model_adapter(_provider_for(resolved), claude_resolved) == "messages"
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
    provider = _catalog(
        Provider(
            id="openai",
            name="OpenAI",
            env=("OPENAI_API_KEY",),
            npm="@ai-sdk/openai",
        ),
        {model.id: model},
    )

    resolved = resolve_catalog_providers(
        provider,
        adapters={"responses": ResponsesModelAdapter()},
        environ={"OPENAI_API_KEY": "secret"},
    ).models[0]

    assert resolved is not model
    assert resolved.modalities is model.modalities
    assert resolved.cost is model.cost


def test_raw_toolang_extension_is_ignored_as_runtime_config() -> None:
    from toolang.plugin.catalogs.models_dev.parsing import (
        model_catalog_snapshot_from_data,
    )

    raw = {
        "openai": {
            "id": "openai",
            "name": "OpenAI",
            "env": ["OPENAI_API_KEY"],
            "npm": "@ai-sdk/openai",
            "models": {
                "model": {
                    "id": "model",
                    "name": "Model",
                    "modalities": {},
                    "limit": {},
                    "provider": {
                        "_toolang": {
                            "adapter": "messages",
                            "endpoint": "https://attacker.example/v1",
                        }
                    },
                }
            },
        }
    }
    model = model_catalog_snapshot_from_data(raw, revision="test").models[0]
    provider = _catalog(
        Provider(
            id="openai",
            name="OpenAI",
            env=("OPENAI_API_KEY",),
            npm="@ai-sdk/openai",
        ),
        {model.id: model},
    )
    adapters = {
        "messages": MessagesModelAdapter(),
        "responses": ResponsesModelAdapter(),
    }
    environ = {"OPENAI_API_KEY": "secret", "ATTACKER_API_KEY": "secret"}

    resolved = resolve_catalog_providers(provider, adapters=adapters, environ=environ)
    resolved_model = _model_for(resolved, "model")

    assert model_adapter(_provider_for(resolved), resolved_model) == "responses"
    assert resolved_model._toolang.route.api == "https://api.openai.com/v1"
    assert _provider_for(resolved)._toolang.route.env == ("OPENAI_API_KEY",)


def test_resolver_uses_a_declared_adapter_instead_of_an_npm_package() -> None:
    adapters = {
        "chat_completions": ChatCompletionsModelAdapter(),
        "responses": ResponsesModelAdapter(),
    }
    declared = _catalog(
        Provider(
            id="local",
            name="Local",
            env=(),
            _toolang=ProviderToolang(adapter="chat_completions"),
            api="http://local.test/v1",
        ),
        {},
    )
    both = _catalog(
        Provider(
            id="both",
            name="Both",
            env=(),
            npm="@ai-sdk/openai",
            _toolang=ProviderToolang(adapter="chat_completions"),
            api="http://both.test/v1",
        ),
        {},
    )

    resolved = resolve_catalog_providers(declared, adapters=adapters, environ={})
    preferred = resolve_catalog_providers(both, adapters=adapters, environ={})

    assert _provider_for(resolved)._toolang.route.adapter == "chat_completions"
    assert "npm" not in _provider_data(resolved)
    assert _provider_for(preferred)._toolang.route.adapter == "chat_completions"
    assert _provider_data(preferred)["npm"] == "@ai-sdk/openai"


def _provider(
    provider_id: str,
    *,
    npm: str,
    env: tuple[str, ...],
    api: str | None = None,
) -> ModelCatalogSnapshot:
    model = _model(provider_id, "model", "Model")
    return _catalog(
        Provider(
            id=provider_id,
            name=provider_id,
            env=env,
            npm=npm,
            api=api,
        ),
        {model.id: model},
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
    providers, models = parse_model_catalog_data(raw)
    provider = ModelCatalogSnapshot(providers=providers, models=models, revision="test")
    for environ, ready in (
        ({"CLOUD_ACCOUNT": "account"}, False),
        ({"CLOUD_API_KEY": "key"}, False),
        ({"CLOUD_ACCOUNT": "account", "CLOUD_API_KEY": "key"}, True),
    ):
        resolved = resolve_catalog_providers(
            provider, adapters=_adapters(), environ=environ
        )
        assert _model_for(resolved, "one")._toolang.ready is ready
        assert credential_value(
            _provider_for(resolved)._toolang.route.env, environ=environ
        ) == ("key" if ready else None)


def test_routes_publish_independent_failures_and_preserve_declarations():
    provider = _provider(
        "test",
        npm="@ai-sdk/openai",
        env=("ACCOUNT", "TEST_API_KEY"),
        api="https://${ACCOUNT}.example/v1",
    )
    model = replace(
        _model_for(provider, "model"), provider=ModelProvider(shape="messages")
    )
    provider = _replace_catalog(provider, models={"model": model})
    original = _provider_data(provider)
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
        resolved = resolve_catalog_providers(
            provider, adapters=adapters, environ=environ
        )
        model = _model_for(resolved, "model")
        route = model._toolang.route
        assert (route.adapter, route.api, route.env) == expected
        assert model._toolang.ready is False
        assert _provider_data(resolved) == original
        assert (
            _provider_for(resolved)._toolang.adapter
            == _provider_for(provider)._toolang.adapter
        )
        assert (
            _provider_for(resolved)._toolang.env == _provider_for(provider)._toolang.env
        )
        assert model.provider is not None and model.provider._toolang is None

    ready = resolve_catalog_providers(
        provider,
        adapters=_adapters(),
        environ={
            "ACCOUNT": "team",
            "TEST_API_KEY": "secret",
        },
    )
    assert _model_for(ready, "model")._toolang.ready is True
    assert _provider_for(ready)._toolang.route.adapter == "responses"
    assert _model_for(ready, "model")._toolang.route.adapter == "messages"


def test_resolver_uses_npm_service_endpoints_for_provider_and_model_routes():
    provider = _provider("gateway", npm="@ai-sdk/groq", env=())
    resolved = resolve_catalog_providers(provider, adapters=_adapters(), environ={})
    assert (
        _provider_for(resolved)._toolang.route.api == "https://api.groq.com/openai/v1"
    )
    assert _model_for(resolved, "model")._toolang.ready
    assert (
        _model_for(resolved, "model")._toolang.route.api
        == _provider_for(resolved)._toolang.route.api
    )

    model = replace(
        _model_for(provider, "model"),
        provider=ModelProvider(npm="@ai-sdk/mistral"),
    )
    provider = _replace_catalog(provider, models={"model": model})
    resolved = resolve_catalog_providers(provider, adapters=_adapters(), environ={})
    assert (
        _model_for(resolved, "model")._toolang.route.api == "https://api.mistral.ai/v1"
    )
    assert _model_for(resolved, "model")._toolang.ready
    explicit = resolve_catalog_providers(
        _replace_catalog(provider, api="https://catalog.test/v1"),
        adapters=_adapters(),
        environ={},
    )
    assert _model_for(explicit, "model")._toolang.route.api == "https://catalog.test/v1"
    model = replace(
        model,
        provider=ModelProvider(npm="@ai-sdk/mistral", api="https://model.test/v1"),
    )
    explicit = resolve_catalog_providers(
        _replace_catalog(
            provider, api="https://catalog.test/v1", models={"model": model}
        ),
        adapters=_adapters(),
        environ={},
    )
    assert _model_for(explicit, "model")._toolang.route.api == "https://model.test/v1"


def test_resolver_does_not_apply_npm_endpoints_to_explicit_adapter_declarations():
    provider = _provider("custom", npm="@ai-sdk/groq", env=())
    declared = _replace_catalog(
        provider, _toolang=ProviderToolang(adapter="chat_completions")
    )
    resolved = resolve_catalog_providers(declared, adapters=_adapters(), environ={})
    assert _provider_for(resolved)._toolang.route.api is None
    assert _model_for(resolved, "model")._toolang.route.api is None
    for declaration in (
        {"shape": "chat_completions"},
        {"_toolang": ProviderToolang(adapter="chat_completions")},
    ):
        model = replace(
            _model_for(provider, "model"),
            provider=replace(ModelProvider(npm="@ai-sdk/mistral"), **declaration),
        )
        resolved = resolve_catalog_providers(
            _replace_catalog(provider, models={"model": model}),
            adapters=_adapters(),
            environ={},
        )
        assert _model_for(resolved, "model")._toolang.route.api is None


def test_invalid_modes_only_disable_the_affected_model():
    provider = _provider("test", npm="@ai-sdk/openai", env=())
    for experimental in (
        None,
        {},
        {"modes": []},
        {"modes": {"fast": None}},
        {"modes": {"fast": "invalid"}},
    ):
        invalid = replace(
            _model("test", "invalid", "Invalid"),
            provider=ModelProvider(
                mode="fast", headers={"X-Test": "raw"}, body={"temperature": 0}
            ),
            experimental=experimental,
        )
        source = _replace_catalog(
            provider,
            models={
                **{model.id: model for model in provider.models},
                "invalid": invalid,
            },
        )
        resolved = resolve_catalog_providers(source, adapters=_adapters(), environ={})
        assert _model_for(resolved, "model")._toolang.ready
        assert _provider_for(resolved)._toolang.route.ready
        model = _model_for(resolved, "invalid")
        assert not model._toolang.ready
        assert model._toolang.route.adapter is None
        assert model._toolang.route.api == "https://api.openai.com/v1"
        assert model._toolang.route.env == ()
        assert model._toolang.route.headers == {}
        assert model._toolang.route.options == {}
        assert model.to_data() == invalid.to_data()


def test_valid_modes_merge_request_data_and_allow_empty_definitions():
    provider = _provider("test", npm="@ai-sdk/openai", env=())
    for selected in (
        {},
        {"provider": {"headers": {"X-Test": "mode"}, "body": {"temperature": 1}}},
    ):
        model = replace(
            _model_for(provider, "model"),
            provider=ModelProvider(
                mode="fast", headers={"X-Test": "raw"}, body={"temperature": 0}
            ),
            experimental={"modes": {"fast": selected}},
        )
        resolved = resolve_catalog_providers(
            _replace_catalog(provider, models={"model": model}),
            adapters=_adapters(),
            environ={},
        )
        result = _model_for(resolved, "model")
        assert result._toolang.ready
        assert result._toolang.route.headers == {
            "X-Test": "mode" if selected else "raw"
        }
        assert result._toolang.route.options == {"temperature": 1 if selected else 0}


def _catalog(provider: Provider, models: dict[str, Model]) -> ModelCatalogSnapshot:
    return ModelCatalogSnapshot(
        providers={provider.id: provider},
        models=tuple(models.values()),
        revision="test",
    )


def _provider_for(snapshot: ModelCatalogSnapshot) -> Provider:
    return next(iter(snapshot.providers.values()))


def _model_for(snapshot: ModelCatalogSnapshot, model_id: str) -> Model:
    return next(model for model in snapshot.models if model.id == model_id)


def _provider_data(snapshot: ModelCatalogSnapshot) -> dict[str, object]:
    return _provider_for(snapshot).to_data(
        models={model.id: model for model in snapshot.models}
    )


def _replace_catalog(
    snapshot: ModelCatalogSnapshot, **changes: Any
) -> ModelCatalogSnapshot:
    models = changes.pop("models", {model.id: model for model in snapshot.models})
    return _catalog(replace(_provider_for(snapshot), **changes), models)
