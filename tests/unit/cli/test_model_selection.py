"""Client-side materialization of effective model selections."""

from __future__ import annotations

import pytest

from toolang.cli.common.model_selection import (
    is_concrete_model_ref,
    materialize_model_selection,
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


@pytest.mark.parametrize(
    "value",
    (
        "openai/gpt-5",
        "openrouter/openai/gpt-5",
        "provider/model:variant",
        "provider/model(variant)",
        "provider/model=variant",
    ),
)
def test_concrete_model_ref_recognizes_canonical_routes(value: str) -> None:
    assert is_concrete_model_ref(value)


@pytest.mark.parametrize(
    "value",
    (
        "gpt-5",
        "openai/*",
        "openai/gpt-?",
        "openai/gpt-5[reasoning]",
        " openai/gpt-5",
        "openai/gpt-5 ",
        '"openai/gpt-5"',
        "/gpt-5",
        "openai/",
    ),
)
def test_concrete_model_ref_rejects_queries_and_noncanonical_values(
    value: str,
) -> None:
    assert not is_concrete_model_ref(value)


def test_model_list_materializes_a_unique_human_facing_value() -> None:
    assert materialize_model_selection(_MODELS, "openai/gpt-5") == "openai/gpt-5"


def test_model_list_accepts_nested_concrete_route_refs() -> None:
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

    assert materialize_model_selection(payload, "openrouter/openai/gpt-5") == (
        "openrouter/openai/gpt-5"
    )


def test_model_list_rejects_an_ambiguous_selection() -> None:
    payload = {
        "items": [
            {"ref": "openai/gpt-5", "name": "GPT-5", "provider": "openai"},
            {"ref": "openai/o3", "name": "o3", "provider": "openai"},
        ]
    }
    with pytest.raises(ValueError, match="ambiguous"):
        materialize_model_selection(payload, "openai")
