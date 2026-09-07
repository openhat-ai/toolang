"""Setup model ordering and independent compaction selection."""

from dataclasses import replace

import pytest

from toolang.base.model_settings import parse_model_body
from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.common.errors import ToolangError
from toolang.plugin.models.collections import ModelCollection, ModelEntry
from toolang.setup.models import DEFAULT_PROVIDERS, order_models, select_compact_model
from toolang.setup.config import resolve_compact_model, resolve_setup_allow


def entry(
    ref: str, *, tools: bool = True, structured: bool | None = True
) -> ModelEntry:
    provider, _, name = ref.partition("/")
    return ModelEntry(
        key=ref,
        ref=ref,
        info=ModelInfo(ref=ref, provider=provider, name=name, model=name, tools=tools),
        target=ModelTarget(
            ref=ref,
            provider=provider,
            name=name,
            model=name,
            adapter="test",
            tools=tools,
            structured_output=structured,
        ),
    )


def test_default_provider_order_never_excludes_or_reorders_provider_models():
    refs = (
        "other/z",
        "other/a",
        *(f"{p}/z" for p in reversed(DEFAULT_PROVIDERS)),
        "openai/a",
        "unknown/a",
    )
    models = ModelCollection(tuple(entry(ref) for ref in refs))
    ordered = order_models(models, None)
    expected = []
    for provider in DEFAULT_PROVIDERS:
        expected.extend(ref for ref in refs if ref.startswith(f"{provider}/"))
    assert ordered.refs() == (*expected, "other/z", "other/a", "unknown/a")
    assert order_models(models, ("*",)).refs() == refs


@pytest.mark.parametrize("query", ["other/*, openai/*", ["other/*", "openai/*"]])
def test_allow_query_string_and_list_preserve_authored_order(query):
    models = ModelCollection((entry("openai/a"), entry("other/b"), entry("google/c")))
    allow = resolve_setup_allow(({"allow": {"models": query}},))
    assert order_models(models, allow.models).refs() == ("other/b", "openai/a")


def test_compact_config_layers_are_independent_complete_model_requests():
    root = {
        "default": {"model": "normal/a effort=high"},
        "compact": {"model": "other/a effort=low"},
    }
    agent = {"compact": {"model": "other/b"}}
    assert resolve_compact_model(({},)) is None
    assert resolve_compact_model((root, agent)) == parse_model_body("other/b")
    assert resolve_compact_model(
        (root, agent), override=parse_model_body("unset")
    ) == parse_model_body("unset")


@pytest.mark.parametrize("value", ["default", "effort=low"])
def test_compact_config_requires_a_model_or_unset(value):
    with pytest.raises(ValueError, match="exact model or unset"):
        resolve_compact_model(({"compact": {"model": value}},))


def test_compact_selection_filters_capabilities_and_preserves_order():
    models = ModelCollection(
        (
            entry("test/no-tools", tools=False),
            entry("test/unknown", structured=None),
            entry("test/no-schema", structured=False),
            entry("test/second"),
            entry("test/first"),
        )
    )
    assert select_compact_model(models, None).ref == "test/second"
    assert select_compact_model(models.match("test/first, *"), None).ref == "test/first"
    assert (
        select_compact_model(models, parse_model_body("test/first")).ref == "test/first"
    )
    for ref in ("test/missing", "test/no-tools", "test/unknown", "test/no-schema"):
        with pytest.raises(ToolangError, match="available, allowed"):
            select_compact_model(models, parse_model_body(ref))
    with pytest.raises(ToolangError, match="disabled"):
        select_compact_model(models, parse_model_body("unset"))
    with pytest.raises(ToolangError, match="requires an allowed model"):
        select_compact_model(ModelCollection(), None)


def test_unknown_catalog_tool_capability_does_not_qualify():
    models = ModelCollection((entry("test/unknown"),))
    views = tuple(replace(view, tool_call=None) for view in models.query_views())
    models = ModelCollection(models.entries, query_views=views)
    with pytest.raises(ToolangError, match="requires an allowed model"):
        select_compact_model(models, None)
