"""State-bound runnable defaults and shared descriptions."""

from hashlib import sha256

import pytest

from toolang.execution.runnables import runnable_descriptions, resolve_agic_routes
from toolang.lang import Program
from toolang.state.state import AgentState, agent_state_revision


def _state(source: str) -> AgentState:
    root = sha256(b"catalog-root").hexdigest()
    home = sha256(source.encode()).hexdigest()
    return AgentState(
        name="alice",
        revision=agent_state_revision(root, home, name="alice"),
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


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("as_state", [False, True])
@pytest.mark.parametrize("preferred", ["chat", "task", "chore"])
def test_main_fallback_preserves_the_selected_kind(kind, as_state, preferred):
    from toolang.execution.runnables import runnable_binding_defaults

    state = _state(f"{kind} main:\n  pass\n")
    program = state if as_state else state.modules["agent"]
    expected = ("main", None) if kind == "agic" else (None, "main")
    assert runnable_binding_defaults(program, None, fallback_agic=preferred) == expected

    state = _state(f"{kind} main:\n  pass\n\n{kind} {preferred}:\n  pass\n")
    program = state if as_state else state.modules["agent"]
    expected = (preferred, None) if kind == "agic" else (None, preferred)
    assert runnable_binding_defaults(program, None, fallback_agic=preferred) == expected
    explicit = ("main", None) if kind == "agic" else (None, "main")
    assert (
        runnable_binding_defaults(program, f"{kind}:main", fallback_agic=preferred)
        == explicit
    )


@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_runnable_docs_agree_in_help_routes_queries_and_input_contract(kind, capsys):
    from dataclasses import replace
    from io import StringIO
    from pathlib import Path

    from toolang.cli.toolang.commands.script import _program_command
    from toolang.execution.runnables import runnable_signature
    from toolang.state.runnable_collections import runnable_dataset

    state = _state(f"""
## Handle the general request.
{kind} main(_: Part[], topic?: Text):
  pass

agic caller:
  hands = main
  handoffs = main

  Choose a route.
""")
    program = state.modules["agent"]
    target = next(
        item for item in (*program.agics, *program.flows) if item.name == "main"
    )
    assert target.input is not None
    input_doc = "Primary request."
    parameter_doc = "Topic details. " * 80
    target = replace(
        target,
        input=replace(target.input, doc=input_doc),
        params=(replace(target.params[0], doc=parameter_doc),),
    )
    program = replace(
        program,
        agics=tuple(
            target if item.name == "main" and kind == "agic" else item
            for item in program.agics
        ),
        flows=tuple(
            target if item.name == "main" and kind == "flow" else item
            for item in program.flows
        ),
    )
    state = replace(state, modules={"agent": program})
    caller = program.find_agic("caller")
    assert caller is not None
    routes = resolve_agic_routes(state, caller)
    (entry,) = runnable_descriptions(state, routes)
    contract = runnable_signature(state, "agent", target)
    assert entry["ref"] == f"{kind}:main"
    assert entry["actions"] == ["run", "execute"]
    assert entry["documentation"] == "Handle the general request."
    assert (
        entry["input"]
        == contract["input"]
        == {"documentation": input_doc, "optional": False, "type": "Part[]"}
    )
    assert (
        entry["parameters"]
        == contract["parameters"]
        == [
            {
                "documentation": parameter_doc[:512],
                "name": "topic",
                "optional": True,
                "type": "Text",
            }
        ]
    )
    item = next(item for item in runnable_dataset(state).items if item.name == "main")
    assert item.description == entry["documentation"]

    command = _program_command(
        program, source_path=Path("demo.too"), source_label="demo.too", stdin=StringIO()
    )
    command.main(
        args=["main", "--help"], prog_name="too run demo.too", standalone_mode=False
    )
    help_text = " ".join(capsys.readouterr().out.split())
    assert input_doc in help_text
    assert " ".join(parameter_doc.split()) in help_text
    assert entry["documentation"] in help_text
