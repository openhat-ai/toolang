"""Flat catalog model ordering and independent compact selection."""

import pytest

from toolang.base.model_settings import parse_model_body
from toolang.base.types.model import Model, ModelToolang
from toolang.common.errors import ToolangError
from toolang.plugin.models.query import apply_model_operations, filter_models
from toolang.setup.config import resolve_compact_model, resolve_setup_allow
from toolang.setup.models import order_models, select_compact_model


def model(
    ref: str,
    *,
    tools: bool | None = True,
    structured: bool | None = True,
    routable: bool = True,
    allowed: bool = True,
) -> Model:
    provider, _, name = ref.partition("/")
    return Model(
        id=name,
        name=name,
        _toolang=ModelToolang(provider=provider, ready=routable, allowed=allowed),
        tool_call=tools,
        structured_output=structured,
    )


def refs(models):
    return tuple(model.ref for model in models)


def test_unset_allow_preserves_bundled_catalog_order():
    models = tuple(
        model(ref)
        for ref in (
            "other/z",
            "openrouter/b",
            "openai/a",
            "other/a",
            "alibaba/c",
        )
    )

    ordered, allowed = order_models(models, None)

    assert refs(ordered) == refs(models)
    assert allowed == frozenset(refs(models))


@pytest.mark.parametrize("query", ["other/*, openai/*", ["other/*", "openai/*"]])
def test_allow_query_orders_matches_and_retains_unmatched(query):
    models = tuple(
        model(ref)
        for ref in ("openai/a", "unknown/z", "other/b", "google/c", "other/a")
    )
    allow = resolve_setup_allow(({"allow": {"models": query}},))

    ordered, allowed = order_models(models, allow.models)

    assert refs(ordered) == (
        "other/b",
        "other/a",
        "openai/a",
        "unknown/z",
        "google/c",
    )
    assert allowed == frozenset({"other/a", "other/b", "openai/a"})
    assert refs(filter_models(ordered, None)) == refs(ordered)


def test_empty_model_allow_retains_records_but_marks_none_allowed():
    models = (model("openai/a"), model("other/b"))
    allow = resolve_setup_allow(({"allow": {"models": []}},))

    ordered, allowed = order_models(models, allow.models)

    assert refs(ordered) == refs(models)
    assert allowed == frozenset()


def test_allow_status_is_separate_from_route_status():
    model_record = model("test/one", routable=True, allowed=False)
    assert model_record._toolang.routable
    assert not model_record._toolang.allowed
    assert not model_record._toolang.effective_ready


def test_model_queries_use_tq_and_keep_branch_order():
    models = (model("openai/a"), model("other/b"), model("openai/c"))
    assert refs(filter_models(models, ("other/*", "openai/*"))) == (
        "other/b",
        "openai/a",
        "openai/c",
    )
    assert refs(filter_models(models, ("*[tool_call]",))) == refs(models)
    assert refs(filter_models(models, ())) == ()


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
    models = (
        model("test/no-tools", tools=False),
        model("test/unknown", structured=None),
        model("test/no-schema", structured=False),
        model("test/second"),
        model("test/first"),
    )
    assert select_compact_model(models, None).ref == "test/unknown"
    assert (
        select_compact_model(filter_models(models, ("test/first", "*")), None).ref
        == "test/first"
    )
    assert (
        select_compact_model(models, parse_model_body("test/first")).ref == "test/first"
    )
    for ref in ("test/unknown", "test/no-schema"):
        assert select_compact_model(models, parse_model_body(ref)).ref == ref
    for ref in ("test/missing", "test/no-tools"):
        with pytest.raises(ToolangError, match="available, allowed"):
            select_compact_model(models, parse_model_body(ref))
    with pytest.raises(ToolangError, match="disabled"):
        select_compact_model(models, parse_model_body("unset"))
    with pytest.raises(ToolangError, match="requires an allowed model"):
        select_compact_model((), None)


def test_unknown_catalog_tool_capability_does_not_qualify():
    models = (model("test/unknown", tools=None),)
    with pytest.raises(ToolangError, match="requires an allowed model"):
        select_compact_model(models, None)


def test_model_directives_apply_tq_set_operations_in_catalog_order():
    models = (
        model("test/one"),
        model("test/two", tools=False),
        model("other/one"),
        model("test/three"),
    )
    selected = apply_model_operations(
        models,
        (
            ("=", ("test/*",)),
            ("-=", ("test/two",)),
            ("+=", ("other/*",)),
        ),
    )
    assert refs(selected) == ("test/one", "other/one", "test/three")


def test_tq_queries_expose_routability_allow_and_effective_ready():
    models = (
        model("test/allowed", routable=True, allowed=True),
        model("test/blocked", routable=True, allowed=False),
        model("test/offline", routable=False, allowed=True),
    )
    assert refs(filter_models(models, ("*[allowed=true]",))) == (
        "test/allowed",
        "test/offline",
    )
    assert refs(filter_models(models, ("*[available=true]",))) == (
        "test/allowed",
        "test/blocked",
    )
    assert refs(filter_models(models, ("*[ready=true]",))) == ("test/allowed",)
    month_model = Model(
        id="month",
        name="Month",
        _toolang=ModelToolang(provider="test", ready=True),
        release_date="2025-04",
    )
    assert refs(
        filter_models((month_model,), ("test/month[release_date=2025-04]",))
    ) == ("test/month",)
    assert refs(filter_models((model("openai/o3"), model("other/o4")), ("o3",))) == (
        "openai/o3",
    )


def test_sequence_predicates_keep_membership_semantics_and_tq_explicit_operators():
    from toolang.plugin.models.collections import ModelCollection

    models = (
        Model(
            id="a",
            name="A",
            _toolang=ModelToolang(provider="test"),
            modalities={"input": ("image", "text")},
        ),
        Model(
            id="b",
            name="B",
            _toolang=ModelToolang(provider="test"),
            modalities={"input": ("text",)},
        ),
        Model(
            id="c",
            name="C",
            _toolang=ModelToolang(provider="test"),
            modalities={"input": ()},
        ),
    )
    legacy = ModelCollection(models)
    for query in ("*[modalities.input=image]", "*[modalities.input!=image]"):
        assert refs(filter_models(models, (query,))) == refs(
            legacy.match(query).entries
        )
    assert refs(filter_models(models, ("*[modalities.input!=image]", "test/c"))) == (
        "test/b",
        "test/c",
    )
    assert refs(filter_models(models, ("*[modalities.input has no image]",))) == (
        "test/b",
        "test/c",
    )


def test_tq_model_query_parity_for_identity_scalar_predicates_and_missing_fields():
    """Selected legacy semantics survive the TQ switch without a setup index."""
    from toolang.plugin.models.collections import ModelCollection

    models = (
        Model(
            id="gpt-5",
            name="GPT",
            _toolang=ModelToolang(provider="openai", ready=True),
            family="gpt",
            tool_call=True,
            limit={"context": 200000},
        ),
        Model(
            id="model/nested",
            name="Nested",
            _toolang=ModelToolang(provider="openrouter", ready=True),
            family=None,
            tool_call=False,
        ),
        Model(
            id="gpt-mini",
            name="Mini",
            _toolang=ModelToolang(provider="local", ready=False),
            family="gpt",
            tool_call=True,
        ),
    )
    legacy = ModelCollection(models)
    for query in (
        "gpt-*",
        "openrouter/model/*",
        '"openrouter/model/nested"',
        '"*/gpt-5"',
        "*[family=null]",
        "*[limit.context>=200000]",
        "*[tool_call=true]",
        "*[available=false]",
    ):
        assert refs(filter_models(models, (query,))) == refs(
            legacy.match(query).entries
        )
    assert refs(filter_models(models, ("local/*", "openai/*", "gpt-*"))) == (
        "local/gpt-mini",
        "openai/gpt-5",
    )
    assert refs(
        apply_model_operations(
            models,
            (("=", ("gpt-*",)), ("-=", ("local/*",)), ("+=", ("local/*",))),
        )
    ) == refs(
        legacy.apply(
            (("=", ("gpt-*",)), ("-=", ("local/*",)), ("+=", ("local/*",)))
        ).entries
    )
