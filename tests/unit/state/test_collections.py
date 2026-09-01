from __future__ import annotations

import toolang.state.collections as collections
from toolang.state.collections import (
    cap_kind_definition,
    cap_table,
    query_cap_views,
)
from toolang.state.state import CapSource, StateCap
from toolang.state.schemas import cap_display_summary
from toolang.state.types import EntryKind


def _cap(kind: EntryKind, name: str) -> StateCap:
    return StateCap(
        kind=kind,
        name=name,
        shape="dir" if kind == "skill" else "file",
        ref=f"root://{kind}s/{name}",
        path=f"files/root/{kind}s/{name}",
        source=CapSource(
            origin="local",
            form="authored",
            path=f"{kind}s/{name}",
            updated_at="2026-08-30T00:00:00Z",
            fingerprint="0" * 64,
        ),
        meta={},
    )


def test_cap_display_summary_uses_metadata_then_bounded_content(tmp_path) -> None:
    definition = tmp_path / "summary.too"
    definition.write_text(
        "First body paragraph.\n\nSecond body paragraph.",
        encoding="utf-8",
    )
    source = CapSource(
        origin="local",
        form="authored",
        path="prompts/summary.too",
        updated_at="2026-08-30T00:00:00Z",
        fingerprint="0" * 64,
    )

    titled = StateCap(
        kind="prompt",
        name="summary",
        shape="file",
        ref="root://prompts/summary",
        path=str(definition),
        source=source,
        meta={"title": "  Preferred\n title  ", "description": "Description"},
    )
    from_content = StateCap(
        kind="prompt",
        name="summary",
        shape="file",
        ref="root://prompts/summary",
        path=str(definition),
        source=source,
        meta={},
    )

    assert cap_display_summary(titled) == "Preferred title"
    assert cap_display_summary(from_content) == "First body paragraph."

    definition.write_text("x" * 300, encoding="utf-8")
    bounded = cap_display_summary(from_content)
    assert bounded is not None
    assert len(bounded) == 256
    assert bounded.endswith("…")


def test_cap_query_fans_out_over_four_base_collections() -> None:
    entries = (
        _cap("prompt", "summary"),
        _cap("psyche", "reviewer"),
        _cap("service", "github"),
        _cap("skill", "reviewer"),
    )

    qualified = query_cap_views(
        entries,
        agent_name="default",
        queries=("skill/reviewer",),
    )
    plural_collection_prefix = query_cap_views(
        entries,
        agent_name="default",
        queries=("skills/reviewer",),
    )
    unqualified = query_cap_views(
        entries,
        agent_name="default",
        queries=("reviewer",),
    )
    predicate = query_cap_views(
        entries,
        agent_name="default",
        queries=("*[scope=root]",),
    )

    assert [(item.kind, item.name) for item in qualified] == [("skill", "reviewer")]
    assert plural_collection_prefix == ()
    assert [(item.kind, item.name) for item in unqualified] == [
        ("psyche", "reviewer"),
        ("skill", "reviewer"),
    ]
    assert [(item.kind, item.name) for item in predicate] == [
        ("prompt", "summary"),
        ("psyche", "reviewer"),
        ("service", "github"),
        ("skill", "reviewer"),
    ]


def test_combined_caps_without_a_query_preserves_aggregate_order() -> None:
    entries = (
        _cap("prompt", "summary"),
        _cap("psyche", "calm"),
        _cap("service", "github"),
        _cap("skill", "reviewer"),
    )

    views = query_cap_views(entries, agent_name="default", queries=None)

    assert [(item.kind, item.name) for item in views] == [
        ("prompt", "summary"),
        ("psyche", "calm"),
        ("service", "github"),
        ("skill", "reviewer"),
    ]


def test_caps_is_not_a_schema_and_existing_tables_stay_unchanged() -> None:
    views = query_cap_views(
        (_cap("skill", "reviewer"),),
        agent_name="default",
        queries=None,
    )

    assert not hasattr(collections, "CAP_SCHEMA")
    assert cap_kind_definition("skill").schema.name == "skills"
    assert cap_kind_definition("skill").schema.identity.bound == ("skill",)
    assert cap_table(views) == (
        ("KIND", "CAP", "ORIGIN", "FORM", "SCOPE", "SOURCE"),
        (("skill", "reviewer", "local", "authored", "root", "skills/reviewer"),),
    )
    assert cap_table(views, kind="skill") == (
        ("SKILL", "ORIGIN", "FORM", "SCOPE", "SOURCE"),
        (("reviewer", "local", "authored", "root", "skills/reviewer"),),
    )
