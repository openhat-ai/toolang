from __future__ import annotations

import toolang.state.collections as collections
from toolang.state.collections import (
    cap_kind_definition,
    cap_table,
    query_cap_views,
)
from toolang.state.state import CapSource, StateCap
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
    assert cap_kind_definition("skill").schema.identity.bound == ("skills",)
    assert cap_table(views) == (
        ("KIND", "CAP", "ORIGIN", "FORM", "SCOPE", "SOURCE"),
        (("skill", "reviewer", "local", "authored", "root", "skills/reviewer"),),
    )
    assert cap_table(views, kind="skill") == (
        ("SKILL", "ORIGIN", "FORM", "SCOPE", "SOURCE"),
        (("reviewer", "local", "authored", "root", "skills/reviewer"),),
    )
