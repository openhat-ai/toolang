from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Literal, TypedDict, cast

import pytest

from toolang.common.errors import ToolangError
from toolang.common.query import (
    CollectionDefinition,
    CollectionSchema,
    ColumnSpec,
    IdentitySpec,
    Match,
    MatchUnion,
    QueryDataset,
    format_query,
    format_query_text,
    resolve_query_sentinels,
)


@dataclass(frozen=True)
class Limits:
    context: int
    output: int | None


@dataclass(frozen=True)
class Modalities:
    input: tuple[str, ...]
    output: tuple[str, ...]


@dataclass(frozen=True)
class ModelView:
    key: str
    provider: str
    model: str
    available: bool
    scope: Literal["local", "remote"]
    family: str | None
    score: float
    cost: Decimal
    released: date
    observed_at: datetime
    limits: Limits
    modalities: Modalities


MODEL_SCHEMA = CollectionSchema.from_type(
    "models",
    ModelView,
    key="key",
    identity=IdentitySpec(
        paths=("provider", "model"),
        labels=("provider", "model"),
        separator="/",
    ),
    exclude=("key", "provider", "model"),
    overlay_types={"route.streaming": bool},
    columns=(
        ColumnSpec("MODEL", ("scope",)),
        ColumnSpec("LIMIT", ("limits.context", "limits.output"), "pair"),
    ),
)


ITEMS = (
    ModelView(
        key="a",
        provider="openai",
        model="gpt-5",
        available=True,
        scope="remote",
        family="gpt",
        score=9.5,
        cost=Decimal("1.25"),
        released=date(2026, 1, 1),
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        limits=Limits(context=200_000, output=64_000),
        modalities=Modalities(input=("text", "image", "pdf"), output=("text",)),
    ),
    ModelView(
        key="b",
        provider="openrouter",
        model="vendor/model/nested",
        available=True,
        scope="remote",
        family=None,
        score=8.0,
        cost=Decimal("0.50"),
        released=date(2025, 6, 1),
        observed_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        limits=Limits(context=128_000, output=None),
        modalities=Modalities(input=("text", "image"), output=("text",)),
    ),
    ModelView(
        key="c",
        provider="local",
        model="gpt-mini",
        available=False,
        scope="local",
        family="gpt",
        score=7.0,
        cost=Decimal("0"),
        released=date(2024, 1, 1),
        observed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        limits=Limits(context=32_000, output=8_000),
        modalities=Modalities(input=("text",), output=("text",)),
    ),
)


class AmountChoice(Enum):
    LOW = Decimal("1.5")


class DayChoice(Enum):
    FIRST = date(2026, 1, 1)


class MomentChoice(Enum):
    START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ChoiceView:
    key: str
    enabled: Literal[True]
    amount: AmountChoice
    day: DayChoice
    moment: MomentChoice


@pytest.fixture
def models() -> QueryDataset[ModelView]:
    return CollectionDefinition(MODEL_SCHEMA).dataset(
        ITEMS,
        overlays={
            "a": {"route": {"streaming": True}},
            "b": {"route": {"streaming": False}},
            "c": {"route": {"streaming": True}},
        },
    )


def identities(
    dataset: QueryDataset[ModelView], query: str | tuple[str, ...]
) -> list[str]:
    return [dataset.schema.identity_for(item) for item in dataset.query(query)]


def test_query_combines_match_union_predicate_and_and_value_or(
    models: QueryDataset[ModelView],
) -> None:
    assert identities(
        models,
        "openrouter/*,*[scope=local;available]",
    ) == ["openrouter/vendor/model/nested"]
    assert identities(models, "*[scope in (local,remote);!available]") == [
        "local/gpt-mini"
    ]


def test_repeated_sequence_predicates_mean_contains_all(
    models: QueryDataset[ModelView],
) -> None:
    assert identities(
        models,
        "*[modalities.input=image;modalities.input=pdf]",
    ) == ["openai/gpt-5"]
    assert identities(models, "*[modalities.input in (image,pdf)]") == [
        "openai/gpt-5",
        "openrouter/vendor/model/nested",
    ]


def test_typed_literals_and_operators(models: QueryDataset[ModelView]) -> None:
    assert identities(
        models,
        "*[limits.context>=128000;cost<1;released>=2025-01-01;score>=8]",
    ) == ["openrouter/vendor/model/nested"]
    assert identities(models, "*[family=null;limits.output=null]") == [
        "openrouter/vendor/model/nested"
    ]
    assert identities(models, "*[route.streaming]") == [
        "openai/gpt-5",
        "local/gpt-mini",
    ]


def test_bool_literal_flags_and_scalar_enum_literals_are_validated() -> None:
    schema = CollectionSchema.from_type(
        "choices",
        ChoiceView,
        key="key",
        identity=IdentitySpec(paths=("key",), labels=("choice",)),
        exclude=("key",),
    )
    item = ChoiceView(
        key="one",
        enabled=True,
        amount=AmountChoice.LOW,
        day=DayChoice.FIRST,
        moment=MomentChoice.START,
    )
    dataset = QueryDataset(schema, (item,))

    assert dataset.query(
        "*[enabled;amount=1.5;day=2026-01-01;moment=2026-01-01T00:00:00Z]"
    ) == (item,)
    with pytest.raises(ToolangError, match="invalid value 'false'"):
        schema.parse("*[!enabled]")


def test_quoted_text_literals_round_trip_and_globs_only_treat_star_question_special(
    models: QueryDataset[ModelView],
) -> None:
    assert identities(models, '*[family="true"]') == []
    assert identities(models, '*[family~="g[pt]"]') == []
    assert format_query(MODEL_SCHEMA.parse('*[family="true"]')) == '*[family="true"]'
    with pytest.raises(ToolangError, match="use a JSON string"):
        MODEL_SCHEMA.parse("*[family=gpt=5]")
    assert format_query(MODEL_SCHEMA.parse('*[family="gpt=5"]')) == '*[family="gpt=5"]'


def test_raw_query_formatting_preserves_nested_and_empty_matches() -> None:
    assert (
        format_query_text('one[family in (a,b)],,two[name="x,y"],')
        == 'one[family in (a,b)], , two[name="x,y"],'
    )


def test_negative_predicate_requires_a_present_value(
    models: QueryDataset[ModelView],
) -> None:
    empty = ModelView(
        key="empty",
        provider="empty",
        model="empty",
        available=True,
        scope="remote",
        family=None,
        score=1.0,
        cost=Decimal("0"),
        released=date(2020, 1, 1),
        observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        limits=Limits(1, 1),
        modalities=Modalities((), ()),
    )
    dataset = CollectionDefinition(MODEL_SCHEMA).dataset(
        (*ITEMS, empty),
        overlays={
            "a": {"route": {"streaming": True}},
            "b": {"route": {"streaming": False}},
            "c": {"route": {"streaming": True}},
            "empty": {"route": {"streaming": False}},
        },
    )
    assert empty not in dataset.query("*[modalities.input!=image]")
    assert ITEMS[1] not in dataset.query("*[family!=gpt]")
    assert identities(dataset, "*[family!=null]") == [
        "openai/gpt-5",
        "local/gpt-mini",
    ]


def test_multi_component_identity_rejects_ambiguous_leading_components() -> None:
    ambiguous = ModelView(
        key="ambiguous",
        provider="open/router",
        model="nested/model",
        available=True,
        scope="remote",
        family=None,
        score=1,
        cost=Decimal("0"),
        released=date(2020, 1, 1),
        observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        limits=Limits(1, 1),
        modalities=Modalities(("text",), ("text",)),
    )

    with pytest.raises(ToolangError, match="leading identity components"):
        QueryDataset(MODEL_SCHEMA, (ambiguous,))

    with pytest.raises(ToolangError, match="bound components"):
        IdentitySpec(
            paths=("model",),
            labels=("provider", "model"),
            separator="/",
            bound=("open/router",),
        )


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("", "cannot be empty"),
        (",openai/*", "empty value"),
        ("openai/*,", "empty value"),
        ("*[]", "cannot be empty"),
        ("*[scope=remote,available]", "invalid"),
        ("*[scope:remote]", "invalid query field name"),
        ("*[missing=value]", "unknown models query field"),
        ("*[scope~=rem*]", "not valid"),
        ("*[available~=true]", "not valid"),
        ("*[available=yes]", "invalid bool literal"),
        ("*[limits.context=1.5]", "invalid integer literal"),
        ("*[limits.context=null]", "not nullable"),
        ("*[family<gpt]", "not valid"),
        ("*[scope in ()]", "empty value"),
        ("*[family=gpt;]", "empty value"),
        ("*[family=gpt]extra", "must be last"),
    ],
)
def test_invalid_and_legacy_queries_fail_before_matching(
    query: str,
    message: str,
) -> None:
    with pytest.raises(ToolangError, match=message):
        MODEL_SCHEMA.parse(query)


def test_query_values_must_be_strings() -> None:
    with pytest.raises(TypeError, match="query values must be strings"):
        MODEL_SCHEMA.parse(cast(tuple[str, ...], (1,)))


def test_identity_rules_cover_qualified_nested_and_quoted_exact(
    models: QueryDataset[ModelView],
) -> None:
    assert identities(models, "gpt-*") == ["openai/gpt-5", "local/gpt-mini"]
    assert identities(models, "openrouter/vendor/*") == [
        "openrouter/vendor/model/nested"
    ]
    assert identities(models, '"openrouter/vendor/model/nested"') == [
        "openrouter/vendor/model/nested"
    ]
    assert identities(models, '"*/gpt-5"') == []


@dataclass(frozen=True)
class RefView:
    key: str
    ref: str


def test_one_component_identity_treats_separators_as_data() -> None:
    schema = CollectionSchema.from_type(
        "refs",
        RefView,
        key="key",
        identity=IdentitySpec(paths=("ref",), labels=("ref",)),
        exclude=("key", "ref"),
    )
    dataset = QueryDataset(
        schema,
        (
            RefView("a", "control://agent/run:1#step.2"),
            RefView("b", "https://example.test/a/b"),
        ),
    )
    assert [item.key for item in dataset.query("control://agent/*")] == ["a"]
    assert [item.key for item in dataset.query('"https://example.test/a/b"')] == ["b"]


def test_identity_matches_includes_bound_components() -> None:
    schema = CollectionSchema.from_type(
        "skills",
        RefView,
        key="key",
        identity=IdentitySpec(
            paths=("ref",),
            labels=("skill", "skill"),
            separator="/",
            bound=("skill",),
        ),
        exclude=("key", "ref"),
    )

    assert schema.identity_matches(("build",), schema.parse("skill/build").matches[0])
    assert schema.identity_matches(("build",), schema.parse("build").matches[0])
    assert not schema.identity_matches(
        ("build",), schema.parse("prompt/build").matches[0]
    )
    with pytest.raises(ToolangError, match="expected 1 identity components"):
        schema.identity_matches(("skill", "build"), schema.parse("build").matches[0])


def test_matches_preserve_base_order_and_deduplicate(
    models: QueryDataset[ModelView],
) -> None:
    assert identities(models, ("local/*", "openai/*", "gpt-*")) == [
        "openai/gpt-5",
        "local/gpt-mini",
    ]


def test_set_operations_use_immutable_base_and_restore_base_order(
    models: QueryDataset[ModelView],
) -> None:
    active = models.query("local/*")
    selected = models.apply(
        (
            ("+=", "openrouter/*"),
            ("-=", "local/*"),
            ("+=", "openai/*"),
            ("=", "*[available]"),
        ),
        active=active,
    )
    assert [item.key for item in selected] == ["a", "b"]
    external = ModelView(
        key="external",
        provider="external",
        model="outside",
        available=True,
        scope="remote",
        family=None,
        score=1,
        cost=Decimal("0"),
        released=date(2020, 1, 1),
        observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        limits=Limits(1, 1),
        modalities=Modalities(("text",), ("text",)),
    )
    assert external not in models.apply((), active=(*active, external))


def test_singular_query_reports_zero_and_ambiguity(
    models: QueryDataset[ModelView],
) -> None:
    assert models.require_one("openai/gpt-5").key == "a"
    with pytest.raises(ToolangError, match="matched no items"):
        models.require_one("missing")
    with pytest.raises(ToolangError, match="ambiguous; matched 2"):
        models.require_one("gpt-*")


def test_schema_drives_help_json_and_canonical_formatting() -> None:
    data = MODEL_SCHEMA.to_data()
    assert data["identity"] == {
        "labels": ["provider", "model"],
        "separator": "/",
        "bound": [],
        "matching": {
            "bare": "case-sensitive glob",
            "quoted": "exact",
            "unqualified": "final component",
        },
    }
    fields = data["fields"]
    assert isinstance(fields, list)
    typed_fields = cast(list[dict[str, object]], fields)
    assert [field["name"] for field in typed_fields] == list(MODEL_SCHEMA.fields)
    assert "columns" not in data
    assert MODEL_SCHEMA.to_json() == MODEL_SCHEMA.to_json()
    assert "route.streaming: bool" in MODEL_SCHEMA.help_text()
    parsed = MODEL_SCHEMA.parse(
        '*[scope in (remote,local);family="two words";!available]'
    )
    assert isinstance(parsed, MatchUnion)
    assert all(isinstance(match, Match) for match in parsed.matches)
    assert (
        format_query(parsed)
        == '*[scope in (remote,local);family="two words";!available]'
    )


def test_table_columns_read_the_same_public_values(
    models: QueryDataset[ModelView],
) -> None:
    headers, rows = models.table(models.query("openai/*"))
    assert headers == ("MODEL", "LIMIT")
    assert rows == (("remote", "200_000 / 64_000"),)


def test_table_formats_currency_pairs_and_env_requirements() -> None:
    @dataclass(frozen=True)
    class DisplayView:
        key: str
        input_cost: Decimal | None
        output_cost: Decimal | None
        requirements: tuple[str, ...]
        missing: tuple[str, ...]

    schema = CollectionSchema.from_type(
        "display",
        DisplayView,
        key="key",
        identity=IdentitySpec(paths=("key",), labels=("item",)),
        columns=(
            ColumnSpec(
                "PRICE",
                ("input_cost", "output_cost"),
                "currency-pair",
            ),
            ColumnSpec("ENV", ("requirements", "missing"), "env"),
        ),
    )
    dataset = QueryDataset(
        schema,
        (
            DisplayView(
                "one",
                Decimal("1.256"),
                Decimal("0"),
                ("PRIMARY", "USER + TOKEN"),
                ("TOKEN",),
            ),
        ),
    )

    assert dataset.table(dataset.items) == (
        ("PRICE", "ENV"),
        (("$1.26 / $0.00", "PRIMARY, USER + TOKEN (missing)"),),
    )


def test_exact_match_quotes_glob_identity() -> None:
    item = ModelView(
        key="glob",
        provider="provider",
        model="model*literal",
        available=True,
        scope="remote",
        family=None,
        score=1,
        cost=Decimal("0"),
        released=date(2020, 1, 1),
        observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        limits=Limits(1, 1),
        modalities=Modalities(("text",), ("text",)),
    )
    assert MODEL_SCHEMA.exact_match_for(item) == '"provider/model*literal"'


def test_policy_sentinels_are_standalone_and_quoted_identity_remains_a_query() -> None:
    assert resolve_query_sentinels(("all",), label="allow models") is None
    assert resolve_query_sentinels(("none",), label="allow models") == ()
    assert resolve_query_sentinels(('"all"',), label="allow models") == ('"all"',)
    with pytest.raises(ToolangError, match="cannot mix queries with all or none"):
        resolve_query_sentinels(("all,openai/*",), label="allow models")
    with pytest.raises(ToolangError, match="cannot mix queries with all or none"):
        resolve_query_sentinels(("openai/*", "none"), label="allow models")


class TypedNested(TypedDict):
    name: str
    enabled: bool


@dataclass(frozen=True)
class TypedMappingView:
    key: str
    nested: TypedNested


def test_typed_mapping_flattens_to_dotted_fields() -> None:
    schema = CollectionSchema.from_type(
        "typed",
        TypedMappingView,
        key="key",
        identity=IdentitySpec(paths=("key",), labels=("key",)),
        exclude=("key",),
    )
    assert tuple(schema.fields) == ("nested.name", "nested.enabled")


def test_typed_mapping_items_and_optional_nested_fields_are_queryable() -> None:
    schema = CollectionSchema.from_type(
        "typed",
        TypedNested,
        key="name",
        identity=IdentitySpec(paths=("name",), labels=("name",)),
        exclude=("name",),
    )
    dataset = QueryDataset(
        schema,
        (
            TypedNested(name="first", enabled=True),
            TypedNested(name="second", enabled=False),
        ),
    )
    assert [item["name"] for item in dataset.query("*[enabled]")] == ["first"]

    @dataclass(frozen=True)
    class OptionalNestedView:
        key: str
        nested: TypedNested | None

    optional_schema = CollectionSchema.from_type(
        "optional nested",
        OptionalNestedView,
        key="key",
        identity=IdentitySpec(paths=("key",), labels=("key",)),
        exclude=("key",),
    )
    assert optional_schema.fields["nested.enabled"].nullable is True
    optional_dataset = QueryDataset(
        optional_schema,
        (
            OptionalNestedView("present", TypedNested(name="value", enabled=True)),
            OptionalNestedView("missing", None),
        ),
    )
    assert [item.key for item in optional_dataset.query("*[nested.enabled=null]")] == [
        "missing"
    ]


@dataclass(frozen=True)
class UnsafeView:
    key: str
    payload: dict[str, object]


def test_unsafe_dynamic_mapping_and_partial_snapshot_are_rejected() -> None:
    with pytest.raises(ToolangError, match="dynamic mapping"):
        CollectionSchema.from_type(
            "unsafe",
            UnsafeView,
            key="key",
            identity=IdentitySpec(paths=("key",), labels=("key",)),
        )
    schema = CollectionSchema.from_type(
        "unsafe",
        UnsafeView,
        key="key",
        identity=IdentitySpec(paths=("key",), labels=("key",)),
        exclude=("payload",),
    )
    with pytest.raises(ToolangError, match="partial snapshot"):
        QueryDataset(schema, (), complete=False)


def test_dataset_validates_unique_keys_and_overlay_values() -> None:
    with pytest.raises(ToolangError, match="duplicate item key"):
        QueryDataset(
            MODEL_SCHEMA,
            (ITEMS[0], ITEMS[0]),
            overlays={"a": {"route": {"streaming": True}}},
        )
    with pytest.raises(ToolangError, match="missing query overlay"):
        QueryDataset(MODEL_SCHEMA, ITEMS[:1])
