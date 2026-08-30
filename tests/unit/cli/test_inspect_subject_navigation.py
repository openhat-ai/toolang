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
                        tool_name="inspect_run",
                        tool_family="inspect",
                        input={"run_id": "run_123"},
                        reasoning="The run record contains the failure.",
                    ),
                ),
            ),
            Message(
                role="tool",
                parts=(
                    ToolResultPart(
                        tool_call_id="tool-1",
                        tool_name="inspect_run",
                        tool_family="inspect",
                        output={"status": "failed"},
                    ),
                ),
            ),
        ],
        tools=(
            ToolDefinition(
                name="inspect_run",
                description="Inspect one run.",
                parameters={"type": "object"},
            ),
        ),
        cont={"cursor": "next"},
    )

    renderables = inspect_commands._model_call_renderables(model_call_to_data(call))
    by_text = {renderable.plain: renderable for renderable in renderables}
    output = "\n".join(renderable.plain for renderable in renderables)
    output_lines = output.splitlines()

    assert "Review the run.\n<important>Preserve this tag.</important>" in output
    assert '<context name="failure">What happened?</context>' in output
    assert "[0] user" in output
    assert "[1] assistant" in output
    assert "[2] tool" in output
    assert "Tool Call · inspect_run" in output
    assert "Reason\nThe run record contains the failure." in output
    assert "Input\nrun_id: run_123" in output
    assert "Tool Result · inspect_run" in output
    assert "Output\nstatus: failed" in output
    assert "Available Tools · 1 tool" in output
    assert "[0] inspect_run" in output
    assert "Continuation\n\ncursor: next" in output
    assert '"messages":' not in output
    assert by_text["[0] user"].style == "dim"
    assert by_text["[1] assistant"].style == "dim"
    assert by_text["[2] tool"].style == "dim"
    assert by_text["Parameters"].style == "dim"
    assert by_text["type: object"].style == "dim"
    for line in (
        "[0] user",
        '<context name="failure">What happened?</context>',
        "[1] assistant",
        "I will inspect it.",
        "Tool Call · inspect_run",
        "run_id: run_123",
        "[2] tool",
        "Tool Result · inspect_run",
        "status: failed",
        "[0] inspect_run",
        "type: object",
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
