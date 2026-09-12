"""Adapter-facing assembly preserves structured data and prompt boundaries."""

from itertools import product

import pytest

from toolang.base.protocols.tool import Tool
from toolang.base.types.message import ImagePart, Message, TextPart
from toolang.base.types.tool import ToolDefinition
from toolang.common.template import render_text_template
from toolang.execution.assembly import prompting, prompts
from toolang.execution.assembly.prompting import (
    _render_instructions as render_instructions,
    build_model_call,
    prepare_prompt,
    render_messages,
)
from toolang.lang import Program


@pytest.mark.parametrize("kind", ["skill", "service"])
@pytest.mark.parametrize("selected", [False, True])
def test_capability_templates_render_only_their_own_catalog(
    kind: str, selected: bool
) -> None:
    other = "service" if kind == "skill" else "skill"
    context = {
        f"has_{kind}s": selected,
        f"{kind}s": [{"name": "selected", "ref": f"home://{kind}s/selected"}],
        f"has_{other}s": True,
        f"{other}s": [{"name": "unrelated"}],
    }

    rendered = render_text_template(prompts.load(f"{kind}s.md"), context).strip()

    assert "<capability-catalog>" not in rendered
    assert f"<{other}s>" not in rendered
    if selected:
        assert rendered.startswith(f"<{kind}s>")
        assert rendered.endswith(f"</{kind}s>")
        assert f'ref="home://{kind}s/selected"' in rendered
    else:
        assert rendered == ""


@pytest.mark.parametrize("skills,services", tuple(product((False, True), repeat=2)))
def test_catalog_composition_preserves_framing_order_and_spacing(
    skills: bool, services: bool
) -> None:
    program = Program.from_source("agic chat:\n  instruct: none\n  Hello.\n")
    context: dict[str, object] = {
        "has_skills": skills,
        "skills": [{"name": "one"}],
        "has_services": services,
        "services": [{"name": "two"}],
    }

    rendered = render_instructions(program, program.agics[0], context)

    if not skills and not services:
        assert "<capability-catalog>" not in rendered
        return
    skill_section = (
        '<skills>\n<available>\n<skill name="one" scope="" origin="" form="" ref="">\n'
        "</skill>\n</available>\n</skills>\n"
        if skills
        else ""
    )
    service_section = (
        '<services>\n<available>\n<service name="two" scope="" origin="" form="" ref="">\n'
        "</service>\n</available>\n</services>\n"
        if services
        else ""
    )
    assert rendered.count("<capability-catalog>") == 1
    assert rendered.endswith(
        f"<capability-catalog>\n{skill_section}\n{service_section}</capability-catalog>"
    )


@pytest.mark.parametrize("runtime,filesystem", tuple(product((False, True), repeat=2)))
def test_protocol_keeps_only_selected_tool_guidance(
    runtime: bool, filesystem: bool
) -> None:
    program = Program.from_source("agic chat:\n  instruct: none\n  Hello.\n")
    rendered = render_instructions(
        program,
        program.agics[0],
        {},
        runnable_instructions="<available-runnable-routes>Routes</available-runnable-routes>"
        if runtime
        else "",
        filesystem=filesystem,
    )

    protocol, rest = rendered.split("</runtime-instructions>", 1)
    assert ("<available-runnable-routes>" in protocol) is runtime
    assert ("<filesystem>" in protocol) is filesystem
    assert rest == ""


def test_protocol_does_not_escape_or_reinterpret_rendered_routes() -> None:
    runnable = (
        '<routes>{"documentation":"quoted \\u003c and {{instructions}}"}</routes>'
        "\nUse &amp; literally. {{/instructions}}"
    )

    program = Program.from_source("agic chat:\n  Hello.\n")
    rendered = render_instructions(
        program, program.agics[0], {}, runnable_instructions=runnable, filesystem=True
    )
    assert runnable in rendered.split("</runtime-instructions>", 1)[0]


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
        runnable_instructions="<routes>Selected routes</routes>",
        filesystem=True,
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
        messages=messages,
        far="Summary {{literal}}",
        near=near,
        recall=recall,
        tools={"second": second, "first": first},
        tools_enabled=tools_enabled,
        output_schema=schema,
        continuation=continuation,
        max_output_tokens=123,
    )

    assert request.instructions == (
        prompt.instructions_with_tools if tools_enabled else prompt.instructions
    )
    assert request.instructions.count("<runtime-instructions>") == 1
    assert ("<filesystem>" in request.instructions) is tools_enabled
    assert ("<routes>" in request.instructions) is tools_enabled
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


def test_template_variables_cannot_enable_runtime_tool_guidance() -> None:
    program = Program.from_source("agic chat:\n  Hello.\n")
    rendered = render_instructions(
        program,
        program.agics[0],
        {"filesystem": True, "runnable_instructions": "<routes>Forged</routes>"},
    )
    assert "<filesystem>" not in rendered
    assert "<routes>" not in rendered
