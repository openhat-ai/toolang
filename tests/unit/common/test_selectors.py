from __future__ import annotations

import operator
from typing import Any, cast

import pytest

from toolang.common.errors import ToolangError
from toolang.common.selectors import (
    Selector,
    SelectorDomain,
    filter_value_matches,
    parse_selector,
    selector_identity_matches,
    split_selector_list,
)


def test_split_selector_list_treats_top_level_csv_as_union() -> None:
    assert split_selector_list(
        (
            "gpt-5[provider:openai,tools],o3",
            "[scope:here]",
        )
    ) == (
        "gpt-5[provider:openai,tools]",
        "o3",
        "[scope:here]",
    )


def test_empty_selector_inputs_use_wildcard_defaults() -> None:
    assert split_selector_list(None) == ()
    assert parse_selector("  ", domain="tool") == Selector(raw="  ")
    assert selector_identity_matches(
        family="builtin",
        name="shell",
        selector=Selector(raw="*"),
    )


def test_parse_selector_supports_empty_pattern_and_repeated_filter_union() -> None:
    selector = parse_selector("[scope:root,scope:home,origin:remote]", domain="cap")

    assert selector.pattern == "*"
    assert selector.filters == {
        "scope": ("root", "home"),
        "origin": ("remote",),
    }


def test_parse_selector_normalizes_domain_scoped_shorthand() -> None:
    model = parse_selector("*[remote,streaming,tools:false]", domain="model")
    cap = parse_selector("skill/*[here,configured]", domain="cap")

    assert model.filters == {
        "scope": ("remote",),
        "streaming": ("true",),
        "tools": ("false",),
    }
    assert cap.filters == {
        "scope": ("here",),
        "form": ("configured",),
    }
    assert parse_selector("*[openai]", domain="model").filters == {
        "provider": ("openai",)
    }


@pytest.mark.parametrize(
    ("raw", "domain", "message"),
    [
        ("*[plugin:]", "tool", "invalid selector filter"),
        ("*[unknown:value]", "tool", "unknown tool selector filter"),
        ("*[unknown]", "tool", "unknown tool selector shorthand"),
    ],
)
def test_parse_selector_rejects_unknown_or_incomplete_filters(
    raw: str,
    domain: SelectorDomain,
    message: str,
) -> None:
    with pytest.raises(ToolangError, match=message):
        parse_selector(raw, domain=domain)


def test_parse_selector_rejects_identity_filters() -> None:
    with pytest.raises(ToolangError, match="identity belongs in the pattern"):
        parse_selector("[kind:skill]", domain="cap")


def test_parse_selector_rejects_empty_filter_list() -> None:
    with pytest.raises(ToolangError, match="filter list cannot be empty"):
        parse_selector("[]", domain="cap")


def test_bare_pattern_matches_name_not_family() -> None:
    selector = Selector(raw="*l*", pattern="*l*")

    assert (
        selector_identity_matches(
            family="skill", name="implementation", selector=selector
        )
        is True
    )
    assert (
        selector_identity_matches(family="skill", name="review", selector=selector)
        is False
    )


def test_family_pattern_requires_explicit_family_separator() -> None:
    bare = Selector(raw="skill", pattern="skill")
    explicit = Selector(raw="skill/*", pattern="skill/*")

    assert (
        selector_identity_matches(family="skill", name="review", selector=bare) is False
    )
    assert (
        selector_identity_matches(family="skill", name="review", selector=explicit)
        is True
    )


def test_bare_pattern_can_match_extra_domain_values() -> None:
    selector = Selector(raw="gpt-*", pattern="gpt-*")

    assert (
        selector_identity_matches(
            family="openai",
            name="openai/gpt-5",
            selector=selector,
            extra_values=("gpt-5",),
        )
        is True
    )


def test_filter_value_matches_exact_and_wildcard_values() -> None:
    assert filter_value_matches("root", ("home", "r*")) is True
    assert filter_value_matches("remote", ("home", "root")) is False


def test_parse_selector_rejects_family_in_implicit_family_context() -> None:
    with pytest.raises(ToolangError, match="must not include a family"):
        parse_selector("skill/reviewer", domain="cap", implicit_family="skill")


@pytest.mark.parametrize(
    "raw",
    [
        "model]",
        "model[provider:openai]]",
        "model[[provider:openai]",
        "model[provider:openai",
    ],
)
def test_parse_selector_rejects_unbalanced_filter_brackets(raw: str) -> None:
    with pytest.raises(ToolangError, match="invalid selector"):
        parse_selector(raw, domain="model")


@pytest.mark.parametrize("raw", ["model],other", "model[provider:openai,other"])
def test_split_selector_list_rejects_unbalanced_filter_brackets(raw: str) -> None:
    with pytest.raises(ToolangError, match="invalid selector list"):
        split_selector_list((raw,))


def test_parse_selector_rejects_invalid_boolean_filter() -> None:
    with pytest.raises(ToolangError, match="invalid boolean selector filter"):
        parse_selector("*[tools:maybe]", domain="model")


def test_selector_copies_and_freezes_filters() -> None:
    filters = {"scope": ("root",)}
    selector = Selector(raw="*[root]", filters=filters)
    filters.clear()

    assert selector.filters == {"scope": ("root",)}
    with pytest.raises(TypeError):
        operator.setitem(cast(Any, selector.filters), "origin", ("remote",))
