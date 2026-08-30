"""Deterministic runnable catalog rendering."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, cast

import pytest

from toolang.execution.runnables import (
    RUNNABLE_CATALOG_MAX_BYTES,
    RUNNABLE_CATALOG_MAX_ENTRIES,
    RUNNABLE_DOCUMENTATION_MAX_CHARS,
    render_runnable_catalog,
    render_runtime_instructions,
    resolve_agic_routes,
)
from toolang.lang import Program
from toolang.state.state import AgentState, agent_state_revision


def _state(source: str) -> AgentState:
    root = sha256(b"catalog-root").hexdigest()
    home = sha256(source.encode()).hexdigest()
    return AgentState(
        revision=agent_state_revision(root, home),
        root_revision=root,
        home_revision=home,
        root_config={},
        home_config={},
        config={},
        caps={},
        modules={"agent": Program.from_source(source)},
        module_sources={"agent": "agent.too"},
        module_digests={"agent": home},
        module_caps={"agent": ()},
    )


def _document(rendered: str) -> dict[str, Any]:
    opening = "<available-runnable-routes>\n"
    closing = "\n</available-runnable-routes>"
    assert rendered.startswith(opening)
    assert rendered.endswith(closing)
    return cast(dict[str, Any], json.loads(rendered[len(opening) : -len(closing)]))


def test_catalog_renders_recursive_struct_once_and_truncates_docs() -> None:
    documentation = "界" * (RUNNABLE_DOCUMENTATION_MAX_CHARS + 20)
    state = _state(
        f"""
struct Node:
  value: Text
  next?: Node
  children: Node[]

## {documentation}
agic inspect(_: Node) -> Node:
  Inspect.

agic caller:
  hands = inspect

  Call.
"""
    )

    caller = state.modules["agent"].find_agic("caller")
    assert caller is not None
    routes = resolve_agic_routes(state, caller)
    rendered = render_runnable_catalog(state, routes)
    document = _document(rendered)
    entries = document["runnables"]
    inspect = next(item for item in entries if item["ref"] == "agic:inspect")

    assert len(inspect["documentation"]) == RUNNABLE_DOCUMENTATION_MAX_CHARS
    assert inspect["actions"] == ["run"]
    assert [item["name"] for item in inspect["structs"]] == ["Node"]
    assert [field["type"] for field in inspect["structs"][0]["fields"]] == [
        "Text",
        "Node",
        "Node[]",
    ]
    assert len(rendered.encode("utf-8")) <= RUNNABLE_CATALOG_MAX_BYTES


def test_catalog_keeps_longest_entry_prefix_and_exact_omitted_count() -> None:
    targets = "\n\n".join(
        f"agic action_{index:02d}:\n  Act."
        for index in range(RUNNABLE_CATALOG_MAX_ENTRIES + 6)
    )
    hands = ", ".join(
        f"action_{index:02d}" for index in range(RUNNABLE_CATALOG_MAX_ENTRIES + 6)
    )
    state = _state(f"{targets}\n\nagic caller:\n  hands = {hands}\n\n  Call.\n")
    caller = state.modules["agent"].find_agic("caller")
    assert caller is not None
    routes = resolve_agic_routes(state, caller)

    first = render_runnable_catalog(state, routes)
    second = render_runnable_catalog(state, routes)
    document = _document(first)

    assert first == second
    assert len(document["runnables"]) == RUNNABLE_CATALOG_MAX_ENTRIES
    assert document["omitted"] == {"count": 6}
    assert len(first.encode("utf-8")) <= RUNNABLE_CATALOG_MAX_BYTES


def test_catalog_requires_explicit_delegation_intent() -> None:
    state = _state("agic action:\n  Act.\n\nagic caller:\n  hands = action\n  Call.")
    caller = state.modules["agent"].find_agic("caller")
    assert caller is not None
    document = _document(
        render_runnable_catalog(state, resolve_agic_routes(state, caller))
    )

    instruction = document["instruction"]
    assert document["authorized"] == {"hands": {"refs": ["agic:action"], "omitted": 0}}
    assert "merely because it is available" in instruction
    assert "future root Run naturally uses the latest valid State" in instruction
    assert "its result is required before the caller can continue" in instruction
    assert "the caller never resumes" in instruction
    assert "Prefer run when either behavior works" in instruction
    assert "current or an ancestor runnable" in instruction
    assert "read its input signature" in instruction
    assert "Do not invent missing required input" in instruction
    assert "unavailable or ambiguous" in instruction
    assert "normal model output with a specific question" in instruction
    assert "After an input validation error, retry only" in instruction
    assert "a JSON string represents one text part" in instruction
    assert '"type":"text","text":"..."' in instruction


def test_authored_runnable_query_filters_typed_fields() -> None:
    state = _state(
        """
agic inspect(_: Text):
  Inspect.

flow verify:
  pass

agic caller:
  hands = ins*[kind=agic;parameters=_], flow:*

  Call.
"""
    )
    caller = state.modules["agent"].find_agic("caller")
    assert caller is not None

    routes = resolve_agic_routes(state, caller)

    assert [route.runnable.ref for route in routes.resolved] == [
        "agic:inspect",
        "flow:verify",
    ]
    document = _document(render_runnable_catalog(state, routes))
    assert document["authorized"] == {
        "hands": {"refs": ["agic:inspect", "flow:verify"], "omitted": 0}
    }


def test_runtime_instructions_omit_catalog_without_authored_routes() -> None:
    state = _state("agic caller:\n  Call.")
    caller = state.modules["agent"].find_agic("caller")
    assert caller is not None

    rendered = render_runtime_instructions(
        state,
        resolve_agic_routes(state, caller),
    )

    assert "<available-runnable-routes>" not in rendered
    assert '"runnables"' not in rendered
    assert "declares no hands or handoffs" in rendered
    assert "Do not call _too__run or _too__execute" in rendered


def test_catalog_rejects_missing_authored_routes() -> None:
    state = _state("agic caller:\n  Call.")
    caller = state.modules["agent"].find_agic("caller")
    assert caller is not None

    with pytest.raises(ValueError, match="requires hands or handoffs"):
        render_runnable_catalog(state, resolve_agic_routes(state, caller))


def test_catalog_byte_limit_stops_before_a_complete_multibyte_entry() -> None:
    documentation = "界" * RUNNABLE_DOCUMENTATION_MAX_CHARS
    targets = "\n\n".join(
        f"## {documentation}\nagic action_{index:02d}:\n  Act."
        for index in range(RUNNABLE_CATALOG_MAX_ENTRIES)
    )
    hands = ", ".join(
        f"action_{index:02d}" for index in range(RUNNABLE_CATALOG_MAX_ENTRIES)
    )
    state = _state(f"{targets}\n\nagic caller:\n  hands = {hands}\n\n  Call.\n")
    caller = state.modules["agent"].find_agic("caller")
    assert caller is not None
    routes = resolve_agic_routes(state, caller)

    rendered = render_runnable_catalog(state, routes)
    document = _document(rendered)
    accepted = document["runnables"]
    total = RUNNABLE_CATALOG_MAX_ENTRIES

    assert 0 < len(accepted) < RUNNABLE_CATALOG_MAX_ENTRIES
    assert document["omitted"] == {"count": total - len(accepted)}
    assert all(len(item["documentation"]) <= 512 for item in accepted)
    assert len(rendered.encode("utf-8")) <= RUNNABLE_CATALOG_MAX_BYTES


def test_catalog_unions_run_and_execute_membership_for_one_target() -> None:
    state = _state(
        """
agic target:
  Target.

agic caller:
  hands = target
  handoffs = agic:target

  Call.
"""
    )
    caller = state.modules["agent"].find_agic("caller")
    assert caller is not None

    document = _document(
        render_runnable_catalog(state, resolve_agic_routes(state, caller))
    )

    assert [(item["ref"], item["actions"]) for item in document["runnables"]] == [
        ("agic:target", ["run", "execute"])
    ]
