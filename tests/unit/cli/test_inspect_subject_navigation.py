"""Typed inspect subject navigation registry tests."""

from __future__ import annotations

import pytest

from toolang.base.types.message import Message, TextPart, ToolCallPart, ToolResultPart
from toolang.base.types.run import ModelCall
from toolang.base.types.tool import ToolDefinition
import toolang.cli.toolang.commands.inspect as inspect_commands
from toolang.execution.records import model_call_to_data


@pytest.mark.parametrize(
    "transition",
    inspect_commands.INSPECT_SUBJECT_TRANSITIONS,
    ids=lambda transition: f"{transition.source}-{transition.name}",
)
def test_registered_subject_transition_drives_dispatch_and_allowed_values(
    transition: inspect_commands._SubjectTransition,
) -> None:
    assert (
        inspect_commands._subject_transition(transition.source, transition.name)
        is transition
    )
    assert transition.name in inspect_commands._allowed_transitions(transition.source)


def test_subject_transition_registry_is_closed_and_unambiguous() -> None:
    registered = {
        (transition.source, transition.name)
        for transition in inspect_commands.INSPECT_SUBJECT_TRANSITIONS
    }

    assert registered == {
        ("agent", "threads"),
        ("agent", "runs"),
        ("thread", "runs"),
        ("run", "steps"),
    }
    assert len(registered) == len(inspect_commands.INSPECT_SUBJECT_TRANSITIONS)


@pytest.mark.parametrize(
    "projector",
    inspect_commands.INSPECT_PROJECTORS,
    ids=lambda projector: f"{projector.source}-{projector.name}",
)
def test_registered_projector_drives_allowed_values(
    projector: inspect_commands._ProjectorTransition,
) -> None:
    assert (
        inspect_commands._projector_transition(projector.source, projector.name)
        is projector
    )
    assert projector.name in inspect_commands._allowed_projectors(projector.source)


def test_inspect_help_is_derived_from_registered_transitions() -> None:
    help_text = inspect_commands._inspect_subject_help()

    for transition in inspect_commands.INSPECT_SUBJECT_TRANSITIONS:
        assert transition.name in help_text
    for projector in inspect_commands.INSPECT_PROJECTORS:
        assert projector.name in help_text


def test_model_call_human_view_preserves_prompts_and_numbers_review_subjects() -> None:
    call = ModelCall(
        instructions="Review the run.\n<important>Preserve this tag.</important>",
        messages=[
            Message.user('<context name="failure">What happened?</context>'),
            Message(
                role="assistant",
                parts=(
                    TextPart("I will inspect it."),
                    ToolCallPart(
                        tool_call_id="tool-1",
                        tool_name="inspect__run",
                        tool_family="inspect",
                        input={
                            "run_id": "run_123",
                            "include": ["steps", "errors"],
                        },
                        reasoning="The run record contains the failure.",
                    ),
                ),
            ),
            Message(
                role="tool",
                parts=(
                    ToolResultPart(
                        tool_call_id="tool-1",
                        tool_name="inspect__run",
                        tool_family="inspect",
                        output={"status": "failed"},
                    ),
                ),
            ),
        ],
        tools=(
            ToolDefinition(
                name="inspect__run",
                description="Inspect one run.",
                parameters={
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "include": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "limit": {"type": ["integer", "null"]},
                    },
                    "required": ["run_id"],
                },
            ),
        ),
        cont={"cursor": "next"},
    )

    renderables = inspect_commands._model_call_renderables(
        model_call_to_data(call),
    )
    by_text = {renderable.plain: renderable for renderable in renderables}
    output = "\n".join(renderable.plain for renderable in renderables)
    output_lines = output.splitlines()

    assert "Review the run.\n<important>Preserve this tag.</important>" in output
    assert '<context name="failure">What happened?</context>' in output
    assert "[0] user" in output
    assert "[1] assistant" in output
    assert "[2] tool" in output
    assert "[0] text" not in output
    assert "I will inspect it." in output
    call_boundary = "<[[ ToolCallPart index=1, id=tool-1"
    result_boundary = "<[[ ToolResultPart index=0, id=tool-1"
    assert call_boundary in output
    invocation = 'inspect.run(run_id: "run_123", include: ["steps", "errors"])'
    assert invocation in output
    assert "Reason\nThe run record contains the failure." in output
    assert "\nInput\n" not in output
    assert result_boundary in output
    assert "status: failed" in output
    assert "\nOutput\n" not in output
    assert output_lines.count("]]>") == 2
    assert "Available Tools · 1 tool" in output
    signature = (
        "[0] inspect.run(run_id: string, include?: string[], limit?: integer | null)"
    )
    assert signature in output
    section_boundary = "░" * 80
    continuation_heading = next(
        renderable
        for renderable in renderables
        if renderable.plain.startswith("Continuation ")
    )
    assert (
        f"{section_boundary}\n{continuation_heading.plain}\n"
        f"{section_boundary}\n\ncursor: next"
    ) in output
    assert '"messages":' not in output
    assert by_text["[0] user"].style == "dim"
    assert by_text["[1] assistant"].style == "dim"
    assert by_text["[2] tool"].style == "dim"
    assert by_text[call_boundary].style == "dim"
    assert by_text[result_boundary].style == "dim"
    assert not by_text[invocation].style
    assert not by_text["status: failed"].style
    assert all(
        renderable.style == "dim"
        for renderable in renderables
        if renderable.plain == "]]>"
    )
    assert by_text[signature].style == "dim"
    for title in (
        "Instructions",
        "Conversation · 3 messages",
        "Available Tools · 1 tool",
        "Continuation",
    ):
        title_index = next(
            index
            for index, renderable in enumerate(renderables)
            if renderable.plain.startswith(f"{title} ")
        )
        assert len(renderables[title_index].plain) == 80
        assert renderables[title_index].plain.endswith("░")
        assert renderables[title_index - 1].plain == section_boundary
        assert renderables[title_index - 1].style == "dim"
        assert renderables[title_index + 1].plain == section_boundary
        assert renderables[title_index + 1].style == "dim"
    conversation_heading = next(
        renderable
        for renderable in renderables
        if renderable.plain.startswith("Conversation · 3 messages ")
    )
    conversation_title_end = len("Conversation")
    conversation_fact_end = len("Conversation · 3 messages")
    assert [
        (span.start, span.end, span.style) for span in conversation_heading.spans
    ] == [
        (0, conversation_title_end, "bold"),
        (conversation_title_end, conversation_fact_end, "dim"),
        (conversation_fact_end, 80, "dim"),
    ]
    tools_heading = next(
        renderable
        for renderable in renderables
        if renderable.plain.startswith("Available Tools · 1 tool ")
    )
    tools_title_end = len("Available Tools")
    tools_fact_end = len("Available Tools · 1 tool")
    assert [(span.start, span.end, span.style) for span in tools_heading.spans] == [
        (0, tools_title_end, "bold"),
        (tools_title_end, tools_fact_end, "dim"),
        (tools_fact_end, 80, "dim"),
    ]
    assert all(renderable.plain != "Model Call" for renderable in renderables)
    for line in (
        "[0] user",
        '<context name="failure">What happened?</context>',
        "[1] assistant",
        "I will inspect it.",
        invocation,
        "[2] tool",
        call_boundary,
        result_boundary,
        "status: failed",
        signature,
    ):
        assert line in output_lines


def test_structured_human_values_are_zero_based_and_keep_nested_shape() -> None:
    renderables = inspect_commands._structured_renderables(
        {"items": ["first", {"enabled": True}]},
        style="dim",
    )

    assert [renderable.plain for renderable in renderables] == [
        "items:",
        "  [0]: first",
        "  [1]:",
        "    enabled: true",
    ]
    assert all(renderable.style == "dim" for renderable in renderables)


def test_human_tool_name_replaces_only_the_toolset_separator() -> None:
    assert (
        inspect_commands._display_tool_name("browser__open__preview")
        == "browser.open__preview"
    )
