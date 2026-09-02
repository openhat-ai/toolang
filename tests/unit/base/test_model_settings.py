from __future__ import annotations

import pytest

from toolang.base.model_settings import (
    apply_model_override,
    compose_model_overrides,
    format_model_body,
    parse_model_body,
)
from toolang.base.types.model import (
    ModelOverride,
    ModelParameters,
    ModelRequest,
    ReasoningParameters,
)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("openai/gpt-5", ModelOverride(identity="openai/gpt-5")),
        ("effort=high", ModelOverride(effort="high")),
        ("effort=4096", ModelOverride(effort=4096)),
        ("effort=auto", ModelOverride(effort="auto")),
        (
            "openai/gpt-5 effort=high",
            ModelOverride(identity="openai/gpt-5", effort="high"),
        ),
        ("DEFAULT", ModelOverride(identity="default")),
        ("UNSET", ModelOverride(identity="unset")),
    ],
)
def test_model_body_parses_canonical_identity_and_effort(
    body: str,
    expected: ModelOverride,
) -> None:
    assert parse_model_body(body) == expected
    assert parse_model_body(format_model_body(expected)) == expected


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("", "requires an identity"),
        ("openai/gpt-5 extra", "identity must be the first"),
        ("temperature=0.2", "unknown model parameter"),
        ("effort=low effort=high", "duplicate model parameter"),
        ("none", "was removed"),
        ("unset effort=high", "unset cannot combine"),
        ("effort=-1", "unknown reasoning effort"),
    ],
)
def test_model_body_rejects_invalid_or_untyped_forms(
    body: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_model_body(body)


def test_model_override_application_preserves_and_resets_typed_parameters() -> None:
    surface = ModelRequest(
        "openai/gpt-5",
        ModelParameters(reasoning=ReasoningParameters(effort="medium")),
    )

    high = apply_model_override(
        surface,
        surface,
        parse_model_body("effort=high"),
    )
    assert high == ModelRequest(
        "openai/gpt-5",
        ModelParameters(reasoning=ReasoningParameters(effort="high")),
    )
    assert apply_model_override(
        high,
        surface,
        parse_model_body("effort=auto"),
    ) == ModelRequest("openai/gpt-5")
    assert apply_model_override(
        high,
        surface,
        parse_model_body("anthropic/claude"),
    ) == ModelRequest("anthropic/claude")
    assert apply_model_override(
        high,
        surface,
        parse_model_body("default effort=low"),
    ) == ModelRequest(
        "openai/gpt-5",
        ModelParameters(reasoning=ReasoningParameters(effort="low")),
    )


def test_setup_source_model_overrides_compose_in_order() -> None:
    assert compose_model_overrides(
        (
            parse_model_body("openai/gpt-5 effort=low"),
            parse_model_body("effort=high"),
        )
    ) == ModelOverride(identity="openai/gpt-5", effort="high")
    assert compose_model_overrides(
        (
            parse_model_body("openai/gpt-5 effort=low"),
            parse_model_body("anthropic/claude"),
        )
    ) == ModelOverride(identity="anthropic/claude")
    assert compose_model_overrides(
        (
            parse_model_body("unset"),
            parse_model_body("default effort=high"),
        )
    ) == ModelOverride(identity="default", effort="high")
