"""Adapter-facing assembly preserves structured data and stable protocol."""

from itertools import product
from xml.etree import ElementTree as ET

import pytest

from toolang.base.protocols.tool import Tool
from toolang.base.types.message import ImagePart, Message, TextPart
from toolang.base.types.tool import ToolDefinition
from toolang.execution.assembly import prompting, prompts
from toolang.execution.assembly.history import assemble_messages
from toolang.execution.assembly.prompting import (
    _render_instructions as render_instructions,
    build_model_call,
    prepare_prompt,
    render_messages,
    resource_declarations,
)
from toolang.lang import Program


@pytest.mark.parametrize("skills,services", tuple(product((False, True), repeat=2)))
def test_resources_are_individual_resident_triggers(skills, services):
    program = Program.from_source("agic chat:\n  instruct: none\n  Hello.\n")
    context = {
        "skills": [
            {
                "ref": "home://skills/one",
                "revision": "a" * 64,
                "description": "Use when testing.",
            }
        ]
        if skills
        else [],
        "services": [
            {
                "ref": "home://services/two",
                "revision": "b" * 64,
                "description": "Use for issues.",
            }
        ]
        if services
        else [],
    }
    rendered = render_instructions(
        program, program.agics[0], context, declarations=resource_declarations(context)
    )
    root = ET.fromstring('<root xmlns:toolang="urn:test">' + rendered + "</root>")
    assert [child.tag.removeprefix("{urn:test}") for child in root] == [
        "protocol",
        *(["skill-trigger"] if skills else []),
        *(["service-trigger"] if services else []),
    ]
    assert "catalog>" not in rendered and "<available>" not in rendered


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
        assert not any(line.startswith("#") for line in protocol.splitlines())
        assert (
            "skill-guidance" in protocol
            and "wait for the guidance user message" in protocol
        )
        assert "pick receipts are not loaded guidance" in protocol


def test_default_context_contains_only_dynamic_public_facts():
    program = Program.from_source("agic chat:\n  Hello.\n")
    context = {
        "agent": {"name": "alice", "home": "/secret"},
        "model": {"provider": "provider", "name": "name", "family": "obsolete"},
        "date": "today",
        "timezone": "UTC",
    }
    result = prompting._render_context(program, program.agics[0], context)
    assert (
        result
        == "<toolang:context>\ndate: today\ntimezone: UTC\nmodel_provider: provider\nmodel_name: name\n</toolang:context>"
    )
    instructions = render_instructions(program, program.agics[0], context)
    assert "You are the alice Toolang agent." in instructions
    assert "/secret" not in instructions and "obsolete" not in instructions


class _DefinitionTool(Tool):
    def __init__(self, definition: ToolDefinition) -> None:
        self.value = definition
        self.calls = 0

    def definition(self) -> ToolDefinition:
        self.calls += 1
        return self.value

    async def invoke(self, arguments, context):
        pytest.fail("Model-call assembly must not execute tools")


@pytest.mark.parametrize("tools_enabled", [False, True])
@pytest.mark.parametrize("recall", [(), ("far",), ("near",), ("far", "near")])
def test_model_call_prepares_all_adapter_fields_without_serializing_tools(
    tools_enabled: bool, recall: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    program = Program.from_source("agic chat:\n  context: none\n  {{_}}\n")
    agic = program.agics[0]
    primary = (
        TextPart("Review {{literal}}"),
        ImagePart(file_id="image-1"),
    )
    rendered, invocations = render_messages(
        program,
        agic.messages,
        values={"_": primary},
        types={"_": "Part[]"},
        definitions={},
    )
    assert not invocations
    prompt = prepare_prompt(
        program,
        agic,
        {},
        rendered=rendered,
        primary=primary,
        runnables=({"ref": "agic:child", "actions": ["run"]},),
    )
    assert prompt.messages == (Message(role="user", parts=primary),)
    near = (Message.user("Earlier {{input}}"), Message.assistant("Earlier reply"))
    messages = [*prompt.messages, Message.user("Current {{input}}")]
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"unique_output": {"type": "string"}},
    }
    continuation = {"adapter": {"id": "response-1"}}
    # Sort by registered key, not definition name; preserve definitions verbatim.
    first = _DefinitionTool(ToolDefinition("zebra", "Unique tool description", schema))
    second = _DefinitionTool(ToolDefinition("ant", "Second description"))

    def unexpected_render(*args, **kwargs):
        pytest.fail("Per-call assembly must not render cached prompts or history again")

    monkeypatch.setattr(prompting, "render_text_template", unexpected_render)
    request = build_model_call(
        prompt,
        messages=assemble_messages("Summary {{literal}}", near, messages, recall),
        tools={"second": second, "first": first},
        tools_enabled=tools_enabled,
        output_schema=schema,
        continuation=continuation,
        max_output_tokens=123,
    )

    assert request.instructions == prompt.instructions
    assert request.instructions.count("<toolang:protocol>") == 1
    assert "<toolang:workspaces>" in request.instructions
    assert "<toolang:runnable-info " in request.instructions
    assert "Unique tool description" not in request.instructions
    assert "unique_output" not in request.instructions
    assert request.messages == [
        *([Message.user("Summary {{literal}}")] if "far" in recall else []),
        *(near if "near" in recall else ()),
        *messages,
    ]
    assert request.messages is not messages
    assert request.messages[-2] is prompt.messages[0]
    assert request.tools == ((first.value, second.value) if tools_enabled else ())
    assert first.calls == second.calls == int(tools_enabled)
    if tools_enabled:
        assert request.tools[0] is first.value
    assert request.output_schema == schema
    assert request.output_schema is not schema
    assert request.output_schema is not None
    assert request.output_schema["properties"] is not schema["properties"]
    assert request.continuation is continuation
    assert request.max_output_tokens == 123
