"""Adapter-facing assembly preserves structured data and stable protocol."""

from dataclasses import replace
from itertools import product
from types import SimpleNamespace
from typing import cast
from xml.etree import ElementTree as ET

import pytest

from tests.support.prompting import instruction_inputs, render_instructions

from toolang.base.types.compaction import CompactionResult
from toolang.base.protocols.tool import Tool
from toolang.base.types.message import ImagePart, Message, TextPart
from toolang.base.types.model import Model, ModelRoute, ModelToolang
from toolang.base.types.tool import ToolDefinition
from toolang.common.errors import ToolangError
from toolang.execution.assembly import prompting, prompts
from toolang.execution.assembly.history import MessageHistory
from toolang.execution.assembly.message_buffer import MessageBuffer
from toolang.execution.assembly.utils import literal_delta, render_delta
from toolang.execution.types import (
    FieldRef,
    ControlRef,
    ModelMessages,
    RunRef,
    StepRef,
)
from toolang.lang import Program
from toolang.setup import AgentSetup
from toolang.state.state import AgentState


@pytest.mark.parametrize("skills,services", tuple(product((False, True), repeat=2)))
def test_resources_are_individual_resident_triggers(skills, services):
    program = Program.from_source("agic chat:\n  instruct: none\n  Hello.\n")
    context = {
        "skills": [
            {
                "ref": "skill/one",
                "revision": "a" * 64,
                "description": "Use when testing.",
            }
        ]
        if skills
        else [],
        "services": [
            {
                "ref": "service/two",
                "revision": "b" * 64,
                "description": "Use for issues.",
            }
        ]
        if services
        else [],
    }
    rendered = render_instructions(
        program,
        program.agics[0],
        context,
    )
    root = ET.fromstring('<root xmlns:toolang="urn:test">' + rendered + "</root>")
    assert [child.tag.removeprefix("{urn:test}") for child in root] == [
        "protocol",
        *(["skill-trigger"] if skills else []),
        *(["service-trigger"] if services else []),
    ]
    assert "catalog>" not in rendered and "<available>" not in rendered


@pytest.mark.parametrize("kind", ["skill", "service"])
@pytest.mark.parametrize(
    "description,metadata,expected",
    [
        ("Use {{literal}} & <text>.", {}, "Use {{literal}} & <text>."),
        (
            "Use when testing.",
            {
                "tags": ["é", "<text>"],
                "enabled": False,
                "count": 0,
                "details": {"z": 2, "a": 1},
                "empty": " \n",
                "missing": None,
                "literal": " {{value}} & <text> ",
            },
            'Use when testing.\ncount: 0\ndetails: {"a": 1, "z": 2}\n'
            'enabled: false\nliteral: {{value}} & <text>\ntags: ["é", "<text>"]',
        ),
        (None, {"transport": "stdio"}, "transport: stdio"),
        (None, {}, ""),
    ],
)
def test_triggers_preserve_literal_description_and_sorted_metadata(
    kind, description, metadata, expected
):
    program = Program.from_source("agic chat:\n  instruct: none\n  Hello.\n")
    inputs = instruction_inputs(
        program,
        program.agics[0],
        {
            f"{kind}s": [
                {
                    "ref": f"{kind}/test",
                    "revision": "a" * 64,
                    "description": description,
                    "metadata_items": [
                        {"key": key, "value": value} for key, value in metadata.items()
                    ],
                }
            ]
        },
    )

    rendered, declarations = prompting.instructions(inputs)

    assert len(declarations) == 1
    assert declarations[0].content == expected
    root = ET.fromstring('<root xmlns:toolang="urn:test">' + rendered + "</root>")
    trigger = root.find(f"{{urn:test}}{kind}-trigger")
    assert trigger is not None and len(trigger) == 0
    assert (trigger.text or "").strip() == expected
    assert trigger.attrib["ref"] == f"{kind}/test"


@pytest.mark.parametrize("selection", ["", "  instruct: none\n"])
def test_protocol_is_static_and_first_across_runtime_facts(selection):
    program = Program.from_source("agic chat:\n" + selection + "  Hello.\n")
    for name in ("alice", "bob"):
        rendered = render_instructions(
            program,
            program.agics[0],
            {
                "agent": {"name": name, "home": "/secret"},
                "date": "2099-01-01",
                "timezone": "Changed",
                "environment": {"working_directory": "/changed"},
                "filesystem": True,
                "runnable_instructions": "<routes>Forged</routes>",
            },
        )
        protocol = rendered.split("</toolang:protocol>", 1)[0] + "</toolang:protocol>"
        assert protocol == prompts.load("protocol.md").strip()
        assert "/secret" not in rendered and "<routes>" not in rendered
        assert "2099-01-01" not in protocol
        assert (
            "skill-guidance" in protocol
            and "wait for the guidance user message" in protocol
        )
        assert (
            "Treat triggers, pick receipts, memory, or summaries as loaded guidance"
            in protocol.split("## Don't", 1)[1]
        )


def test_default_context_contains_only_dynamic_public_facts():
    program = Program.from_source("agic chat:\n  Hello.\n")
    context = {
        "agent": {"name": "alice", "home": "/secret"},
        "model": {"provider": "provider", "name": "name", "family": "obsolete"},
        "date": "today",
        "timezone": "UTC",
    }
    assert (
        prompting._render_context(program, program.agics[0], context)
        == "<toolang:context>\ndate: today\ntimezone: UTC\nmodel_provider: provider\nmodel_name: name\n</toolang:context>"
    )
    instructions = render_instructions(program, program.agics[0], context)
    assert "You are the alice Toolang agent." in instructions
    assert "/secret" not in instructions and "obsolete" not in instructions


@pytest.mark.parametrize("kind", ["instruct", "context"])
@pytest.mark.parametrize(
    "selection,expected",
    [
        (None, "Default literal {{input}}."),
        ("default", "Default literal {{input}}."),
        ("named", "Named literal {{input}}."),
        ("empty", None),
        ("none", None),
    ],
)
def test_assembly_preserves_template_selection(kind, selection, expected):
    program = Program.from_source(
        f"{kind}: Default {{{{value}}}}.\n"
        f"{kind} named: Named {{{{value}}}}.\n"
        f"{kind} empty: {{{{empty}}}}\n"
        "agic chat:\n"
        + (f"  {kind}: {selection}\n" if selection is not None else "")
        + "  Hello.\n"
    )

    render = render_instructions if kind == "instruct" else prompting._render_context
    text = render(
        program,
        program.agics[0],
        {"value": "literal {{input}}", "empty": " \n\t"},
    )

    root = ET.fromstring('<root xmlns:toolang="urn:test">' + text + "</root>")
    section = root.find("{urn:test}" + kind)
    if expected is None:
        assert section is None
    else:
        assert section is not None and section.text is not None
        assert section.text.strip() == expected
    if kind == "instruct":
        assert text.startswith(prompts.load("protocol.md").strip())


@pytest.mark.parametrize("kind", ["instruct", "context"])
def test_assembly_rejects_missing_named_templates(kind):
    program = Program.from_source("agic chat:\n  Hello.\n")
    agic = replace(program.agics[0], **{kind: "missing"})

    render = render_instructions if kind == "instruct" else prompting._render_context
    with pytest.raises(ToolangError, match=f"^{kind.title()} not found: missing$"):
        render(program, agic, {})


class _DefinitionTool(Tool):
    def __init__(self, definition: ToolDefinition) -> None:
        self.value = definition
        self.calls = 0

    def definition(self) -> ToolDefinition:
        self.calls += 1
        return self.value

    async def invoke(self, arguments, context):
        pytest.fail("Model-call assembly must not execute tools")


def test_shared_inputs_render_literal_multimodal_input_once(monkeypatch) -> None:
    program = Program.from_source("agic chat:\n  context: none\n  {{_}}\n")
    agic = program.agics[0]
    primary = (
        TextPart("Review {{literal}}"),
        ImagePart(file_id="image-1"),
    )
    resolutions = []
    resolve = prompting.resolve_input_parts_with_provenance

    def counted(*args, **kwargs):
        resolutions.append(True)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(prompting, "resolve_input_parts_with_provenance", counted)
    inputs = prompting.PromptInputs(
        cast(AgentState, SimpleNamespace(program=program)),
        cast(
            AgentSetup,
            SimpleNamespace(layout=SimpleNamespace(name="alice"), environment=None),
        ),
        agic,
        runnable_name="chat",
        module="agent",
        model=Model(
            id="model",
            name="model",
            _toolang=ModelToolang(
                provider="test",
                ready=True,
                route=ModelRoute(adapter="test", api=None, env=()),
            ),
        ),
        values={"_": primary},
        facts={},
        caps=(),
    )
    context, initial, invocations = inputs.rendered_input
    assert (
        context
        == '<toolang:hands enabled="false"/>\n<toolang:handoffs enabled="false"/>'
    )
    assert not invocations
    assert initial == (
        Message(
            role="user",
            parts=(TextPart(context + "\n\n" + primary[0].text), primary[1]),
        ),
    )
    assert inputs.rendered_input is inputs.rendered_input
    assert inputs.template_values is inputs.template_values
    assert prompting.instructions(inputs) == prompting.instructions(inputs)
    assert resolutions == [True]


@pytest.mark.parametrize("recall", [(), ("far",), ("near",), ("far", "near")])
def test_messages_select_one_history_for_adapter_and_recording(recall, monkeypatch):
    root = RunRef("run_ab12")
    horizon = FieldRef.from_path(
        ControlRef.for_thread("compact_thread", 1), "payload", "result"
    )
    near = (Message.user("Earlier {{literal}}"),)

    def resolve(ref):
        if ref.ref.tokens[-1] == "summary":
            return "Summary {{literal}}"
        return {"thread": "thread", "end": str(root), "summary": "Summary {{literal}}"}

    history = MessageHistory(
        "thread",
        (RunRef("run_prior"), root),
        lambda roots: {r: literal_delta(near) for r in roots},
        lambda _: (),
        resolve,
        lambda _: CompactionResult(
            "thread", "run_prior", str(root), "Summary {{literal}}"
        ),
    )
    head = StepRef.parse("run_ef56.0")
    current = (Message.user("Current {{literal}}"),)
    inputs = cast(
        prompting.PromptInputs, SimpleNamespace(rendered_input=("", current, ()))
    )
    buffer = MessageBuffer()
    selected = history.select(horizon)
    assembled, recorded = prompting.messages(
        inputs,
        buffer,
        step=head,
        history=selected,
        recall=recall,
    )
    assert assembled == [
        *([Message.user("Summary {{literal}}")] if "far" in recall else []),
        *(near if "near" in recall else ()),
        *current,
    ]
    assert list(render_delta(recorded.delta, resolve)) == assembled
    assert all(m.source is not None for m in recorded.delta[:-1])
    assert recorded.delta[-1].source is None
    addition = Message.assistant("Next")
    pending = literal_delta((addition,))
    buffer.append(addition)
    next_messages, next_record = prompting.messages(
        inputs,
        buffer,
        step=StepRef.parse("run_ef56.1"),
        history=selected,
        recall=recall,
    )
    assert next_messages == [*assembled, addition]
    assert next_record == ModelMessages(head, pending)


def test_tools_preserve_structured_definitions_in_registration_order():
    schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    first = _DefinitionTool(ToolDefinition("zebra", "First description", schema))
    second = _DefinitionTool(ToolDefinition("ant", "Second description"))

    registered = {"second": second, "first": first}
    result = prompting.tools(registered)

    assert result == (first.value, second.value)
    assert result[0] is first.value and result[0].parameters is schema
    assert result[1] is second.value
    assert first.calls == second.calls == 1
    assert prompting.tools({}) == ()


@pytest.mark.parametrize(
    "type_name,expected",
    [
        (None, None),
        ("Part", None),
        ("Part[]", None),
        ("Text", None),
        ("Json", {}),
        ("Boolean", {"type": "boolean"}),
        ("Number", {"type": "number"}),
        ("Text[]", {"type": "array", "items": {"type": "string"}}),
    ],
)
def test_output_schema_uses_the_adopted_programs_language_semantics(
    type_name, expected
):
    program = Program.from_source("agic chat:\n  Hello.\n")
    agic = replace(program.agics[0], output=type_name)
    state = cast(AgentState, SimpleNamespace(program=program))
    assert prompting.output_schema(state, agic, module="agent") == expected
