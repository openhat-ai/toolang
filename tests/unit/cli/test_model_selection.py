"""Client-side materialization of effective model-list refs."""

from __future__ import annotations

import pytest

from toolang.cli.common.model_selection import (
    materialize_model_list_ref,
    model_ref_is_exact_route,
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
    assert materialize_model_list_ref(_MODELS, "*[reasoning:true]") == "openai/gpt-5"


def test_model_list_rejects_an_ambiguous_selector() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        materialize_model_list_ref(_MODELS, "gpt-5")


def test_exact_model_route_detection_excludes_selector_syntax() -> None:
    assert model_ref_is_exact_route("openai/gpt-5")
    assert not model_ref_is_exact_route("openai/gpt-*")
    assert not model_ref_is_exact_route("gpt-5[openai]")
    assert not model_ref_is_exact_route("configured-alias")
