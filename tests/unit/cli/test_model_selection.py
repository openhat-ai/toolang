"""Client-side materialization of effective model-list refs."""

from __future__ import annotations

import pytest

from toolang.cli.common.model_selection import (
    materialize_model_list_ref,
)


_MODELS = {
    "default": "openai/gpt-5",
    "items": [
        {
            "ref": "openai/gpt-5",
            "name": "GPT-5",
            "provider": "openai",
            "parameters": {"reasoning": {"effort": ["low", "high"]}},
        },
        {
            "ref": "anthropic/gpt-5",
            "name": "GPT-5 Compatible",
            "provider": "anthropic",
            "parameters": {"reasoning": {"effort": []}},
        },
    ],
}


def test_model_list_materializes_legacy_provider_filters() -> None:
    assert materialize_model_list_ref(_MODELS, "gpt-5[openai]") == "openai/gpt-5"


def test_model_list_prefers_exact_routes_and_accepts_nested_legacy_routes() -> None:
    payload = {
        "items": [
            *_MODELS["items"],
            {
                "ref": "openrouter/openai/gpt-5",
                "name": "GPT-5 through OpenRouter",
                "provider": "openrouter",
                "parameters": {"reasoning": {"effort": ["low", "high"]}},
            },
        ]
    }

    assert materialize_model_list_ref(payload, "openai/gpt-5") == "openai/gpt-5"
    assert (
        materialize_model_list_ref(payload, "openai/gpt-5[openrouter]")
        == "openrouter/openai/gpt-5"
    )
    assert (
        materialize_model_list_ref(payload, "gpt-5[openrouter]")
        == "openrouter/openai/gpt-5"
    )


def test_model_list_rejects_an_ambiguous_selector() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        materialize_model_list_ref(_MODELS, "gpt-5")
