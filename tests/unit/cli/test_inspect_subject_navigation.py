"""Typed inspect subject navigation registry and call rendering tests."""

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
        ("agent", "controls"),
        ("thread", "runs"),
        ("run", "steps"),
        ("step", "runs"),
        ("step", "steps"),
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


def test_projector_registry_uses_distinct_run_and_step_vocabulary() -> None:
    assert {
        (item.source, item.name) for item in inspect_commands.INSPECT_PROJECTORS
    } == {
        ("run", "tree"),
        ("step", "call"),
    }
    assert "model-call" not in inspect_commands._PROJECTOR_NAMES


def test_tree_activity_preserves_run_statement_operands_and_aligns_tags() -> None:
    assert inspect_commands._tree_activity("run", None, "flow:parent") == (
        "<flow>  parent"
    )
    assert inspect_commands._tree_activity("step", "model", "openai/gpt-5") == (
        "[model] openai/gpt-5"
    )
    assert (
        inspect_commands._tree_activity("step", "run", "scatter 6 agic:expand_queries")
        == "[run]   scatter 6 <agic>  expand_queries"
    )


@pytest.mark.parametrize(
    ("status", "marker", "style"),
    (
        ("pending", "•", "dim"),
        ("running", "•", "cyan"),
        ("succeeded", "✔", "green"),
        ("failed", "✖", "red"),
        ("canceled", "✖", "yellow"),
    ),
)
def test_status_activity_uses_the_approved_marker_vocabulary(
    status: str,
    marker: str,
    style: str,
) -> None:
    rendered = inspect_commands._status_activity(status, "[model] test")

    assert rendered.plain == f"{marker} [model] test"
    assert [(span.start, span.end, span.style) for span in rendered.spans] == [
        (0, 1, style)
    ]


def test_execution_table_does_not_truncate_reusable_pointers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    pointer = f"run_{'x' * 4_200}.0"

    inspect_commands._echo_execution_table(
        ("RUN STEP", "STATUS"),
        ((pointer, "succeeded"),),
    )

    output = capsys.readouterr().out
    assert pointer in output
    assert "…" not in output


def test_model_call_human_view_is_complete_and_follows_call_lifecycle() -> None:
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
                        output={
                            "status": "failed",
                            "exit_code": 2,
                            "message": "Run inspection failed.",
                        },
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
                        "include": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": ["integer", "null"]},
                    },
                    "required": ["run_id"],
                },
            ),
        ),
        output_schema={"type": "boolean"},
        continuation={"cursor": "next"},
    )

    renderables = inspect_commands._model_call_renderables(
        model_call_to_data(call),
        result_parts=({"type": "text", "text": "Final answer."},),
        result_pointer="run_123.4/output/value",
    )
    output = "\n".join(renderable.plain for renderable in renderables)

    assert "Review the run. <important>Preserve this tag.</important>" in output
    assert "2 lines · 57 chars" in output
    assert '[0] user  <context name="failure">What happened?</context>' in output
    assert "[1] assistant  I will inspect it." in output
    invocation = 'inspect.run(run_id: "run_123", include: ["steps", "errors"])'
    assert invocation in output
    assert "The run record contains the failure." not in output
    assert "tool result tool-1 · status=failed · exit_code=2" in output
    assert '{message: "Run inspection failed."}' in output
    signature = (
        "[0] inspect.run(run_id: string, include?: string[], limit?: integer | null)"
    )
    assert signature in output
    assert "Inspect one run." not in output
    assert '{type: "boolean"}' in output
    assert '{cursor: "next"}' in output
    assert "Result run_123.4/output/value" in output
    assert "[=] assistant  Final answer." in output
    section_titles = (
        "Instructions",
        "Messages 3",
        "Tools 1",
        "Output Contract",
        "Continuation",
        "Result run_123.4/output/value",
    )
    section_indexes = [
        next(
            index
            for index, renderable in enumerate(renderables)
            if renderable.plain.startswith(f"{title} ")
        )
        for title in section_titles
    ]
    assert section_indexes == sorted(section_indexes)


def test_model_call_human_view_omits_empty_sections() -> None:
    call = ModelCall(instructions="", messages=[], output_schema=None)

    assert inspect_commands._model_call_renderables(model_call_to_data(call)) == ()


def test_model_call_human_view_shows_every_tool_signature() -> None:
    call = ModelCall(
        instructions="",
        messages=[],
        tools=tuple(
            ToolDefinition(
                name=f"tools__tool_{index}",
                description=f"Tool description {index}",
                parameters={},
            )
            for index in range(14)
        ),
    )

    output = "\n".join(
        renderable.plain
        for renderable in inspect_commands._model_call_renderables(
            model_call_to_data(call)
        )
    )

    assert "[13] tools.tool_13()" in output
    assert "Tool description 13" not in output


def test_model_call_human_view_does_not_truncate_request_text_or_result() -> None:
    instructions = f"instructions start\n{'i' * 200}\ninstructions end"
    message = f"message start\n{'m' * 200}\nmessage end"
    result = f"result start\n{'r' * 200}\nresult end"
    call = ModelCall(
        instructions=instructions,
        messages=[Message.user(message)],
        tools=(
            ToolDefinition(
                name="tools__complete",
                description=f"description start {'d' * 200} description end",
                parameters={
                    "type": "object",
                    "description": f"schema start {'s' * 200} schema end",
                },
            ),
        ),
        output_schema={"description": f"output start {'o' * 200} output end"},
        continuation={"cursor": f"cursor start {'c' * 200} cursor end"},
    )

    output = "\n".join(
        renderable.plain
        for renderable in inspect_commands._model_call_renderables(
            model_call_to_data(call),
            result_parts=({"type": "text", "text": result},),
            result_pointer="run_complete.0/output/value",
        )
    )

    for exact_value in (
        " ".join(instructions.split()),
        " ".join(message.split()),
        " ".join(result.split()),
        "output end",
        "cursor end",
    ):
        assert exact_value in output
    assert "description end" not in output
    assert "schema end" not in output


def test_model_call_messages_are_zero_based_and_result_is_last() -> None:
    call = ModelCall(
        instructions="",
        messages=[Message.user(str(index)) for index in range(10)],
    )

    renderables = inspect_commands._model_call_renderables(
        model_call_to_data(call),
        result_parts=({"type": "text", "text": "result"},),
        result_pointer="run_test.0/output/value",
    )

    assert any(
        renderable.plain.startswith("Messages 10 ") for renderable in renderables
    )
    assert [
        renderable.plain.split("  ", maxsplit=1)[0]
        for renderable in renderables
        if renderable.plain.startswith("[")
    ] == [*(f"[{index}] user" for index in range(10)), "[=] assistant"]
    result_index = next(
        index
        for index, renderable in enumerate(renderables)
        if renderable.plain == "[=] assistant  result"
    )
    assert result_index == len(renderables) - 1


def test_human_tool_name_replaces_only_the_toolset_separator() -> None:
    assert (
        inspect_commands._display_tool_name("browser__open__preview")
        == "browser.open__preview"
    )


def test_tool_result_human_view_keeps_complete_output_and_error_context() -> None:
    renderables = inspect_commands._tool_part_renderables(
        {
            "type": "tool_result",
            "tool_call_id": "tool-error",
            "tool_name": "shell__execute",
            "tool_family": "shell",
            "output": {
                "exit_code": 1,
                "ok": False,
                "stdout": "partial output",
                "nested": {"items": ["first", "last"]},
            },
            "error": "Command failed.",
        },
        result=True,
        index=2,
    )

    assert [renderable.plain for renderable in renderables] == [
        "[2] Tool result tool-error · exit_code=1 · ok=false",
        '{stdout: "partial output", nested: {items: ["first", "last"]}}',
        "Error        Command failed.",
    ]
    assert renderables[-1].style == "red"
