"""Deterministic runnable catalog rendering."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, cast

from toolang.execution.runnables import (
    RUNNABLE_CATALOG_MAX_BYTES,
    RUNNABLE_CATALOG_MAX_ENTRIES,
    RUNNABLE_DOCUMENTATION_MAX_CHARS,
    render_runnable_catalog,
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
        program_source="agents/alice/agent.too",
        program=Program.from_source(source),
        caps=(),
    )


def _document(rendered: str) -> dict[str, Any]:
    opening = "<available-runnables>\n"
    closing = "\n</available-runnables>"
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
"""
    )

    rendered = render_runnable_catalog(state)
    document = _document(rendered)
    entries = document["runnables"]
    inspect = next(item for item in entries if item["ref"] == "agic:inspect")

    assert len(inspect["documentation"]) == RUNNABLE_DOCUMENTATION_MAX_CHARS
    assert [item["name"] for item in inspect["structs"]] == ["Node"]
    assert [field["type"] for field in inspect["structs"][0]["fields"]] == [
        "Text",
        "Node",
        "Node[]",
    ]
    assert len(rendered.encode("utf-8")) <= RUNNABLE_CATALOG_MAX_BYTES


def test_catalog_keeps_longest_entry_prefix_and_exact_omitted_count() -> None:
    source = "\n\n".join(
        f"agic action_{index:02d}:\n  Act."
        for index in range(RUNNABLE_CATALOG_MAX_ENTRIES + 6)
    )

    first = render_runnable_catalog(_state(source))
    second = render_runnable_catalog(_state(source))
    document = _document(first)

    assert first == second
    assert len(document["runnables"]) == RUNNABLE_CATALOG_MAX_ENTRIES
    assert document["omitted"] == {"count": 7}
    assert len(first.encode("utf-8")) <= RUNNABLE_CATALOG_MAX_BYTES


def test_catalog_requires_explicit_delegation_intent() -> None:
    document = _document(render_runnable_catalog(_state("agic action:\n  Act.")))

    instruction = document["instruction"]
    assert "user explicitly asks" in instruction
    assert "authored instructions explicitly require delegation" in instruction
    assert "merely because it resembles the current request" in instruction
    assert "current or an ancestor runnable" in instruction


def test_catalog_byte_limit_stops_before_a_complete_multibyte_entry() -> None:
    documentation = "界" * RUNNABLE_DOCUMENTATION_MAX_CHARS
    source = "\n\n".join(
        f"## {documentation}\nagic action_{index:02d}:\n  Act."
        for index in range(RUNNABLE_CATALOG_MAX_ENTRIES)
    )

    rendered = render_runnable_catalog(_state(source))
    document = _document(rendered)
    accepted = document["runnables"]
    total = RUNNABLE_CATALOG_MAX_ENTRIES + 1  # implicit default agic

    assert 0 < len(accepted) < RUNNABLE_CATALOG_MAX_ENTRIES
    assert document["omitted"] == {"count": total - len(accepted)}
    assert all(len(item["documentation"]) <= 512 for item in accepted)
    assert len(rendered.encode("utf-8")) <= RUNNABLE_CATALOG_MAX_BYTES
