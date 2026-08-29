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


def test_model_call_markdown_presents_messages_as_a_reviewable_dialogue() -> None:
    call = ModelCall(
        instructions="Review the run.",
        messages=[
            Message.user("What happened?"),
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

    markdown = inspect_commands._model_call_markdown(model_call_to_data(call))

    assert "## Instructions\n\nReview the run." in markdown
    assert "### 1. User\n\n> What happened?" in markdown
    assert "### 2. Assistant\n\n> I will inspect it." in markdown
    assert "#### Tool Call: `inspect_run`" in markdown
    assert "**Reasoning**\n\n> The run record contains the failure." in markdown
    assert '**Input**\n\n```json\n{\n  "run_id": "run_123"\n}\n```' in markdown
    assert "### 3. Tool" in markdown
    assert "#### Tool Result: `inspect_run`" in markdown
    assert '**Output**\n\n```json\n{\n  "status": "failed"\n}\n```' in markdown
    assert "## Tools\n\n### `inspect_run`" in markdown
    assert "## Continuation" in markdown
    assert '"messages":' not in markdown


def test_model_call_markdown_uses_safe_fences_for_backticks() -> None:
    assert inspect_commands._markdown_json_block({"value": "```"}) == (
        '````json\n{\n  "value": "```"\n}\n````'
    )
    assert inspect_commands._markdown_code_span("`tool`") == "`` `tool` ``"
