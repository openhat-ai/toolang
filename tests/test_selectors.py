from __future__ import annotations

import pytest

from toolang.base.error import ToolangError
from toolang.selectors import parse_selector, split_selector_list


def test_split_selector_list_treats_top_level_csv_as_union() -> None:
    assert split_selector_list(("gpt-5[provider:openai,tools],o3", "[scope:here]",)) == (
        "gpt-5[provider:openai,tools]",
        "o3",
        "[scope:here]",
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
    cap = parse_selector("skill/*[here,wired]", domain="cap")

    assert model.filters == {
        "scope": ("remote",),
        "streaming": ("true",),
        "tools": ("false",),
    }
    assert cap.filters == {
        "scope": ("here",),
        "form": ("wired",),
    }


def test_parse_selector_rejects_identity_filters() -> None:
    with pytest.raises(ToolangError, match="identity belongs in the pattern"):
        parse_selector("[kind:skill]", domain="cap")


def test_parse_selector_rejects_empty_filter_list() -> None:
    with pytest.raises(ToolangError, match="filter list cannot be empty"):
        parse_selector("[]", domain="cap")


def test_parse_selector_rejects_family_in_implicit_family_context() -> None:
    with pytest.raises(ToolangError, match="must not include a family"):
        parse_selector("skill/reviewer", domain="cap", implicit_family="skill")
