from __future__ import annotations

import pytest

from toolang.base.types.message import TextPart
from toolang.cli.toolang.commands.chat.input import (
    QuickCommand,
    RunOverrideHelp,
    normalize_chat_input,
    parse_chat_input,
    slash_command_name,
)
from toolang.execution.types import ModelOverride, LimitOverride, RunOverride
from toolang.lang.ast import Program
from toolang.lang.input import NamedInputSource, RunnableInputRaw, resolve_input_parts


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("/help", QuickCommand("help")),
        ("/output run_1", QuickCommand("output", "run_1")),
        ("/show run_1", QuickCommand("show", "run_1")),
        ("/model", QuickCommand("model")),
        ("/model openai/gpt-5", QuickCommand("model", "openai/gpt-5")),
        ("/agic", QuickCommand("agic")),
        ("/flow research", QuickCommand("flow", "research")),
        ("/runnable", QuickCommand("runnable")),
        ("/allow models=openai/*", QuickCommand("allow", "models=openai/*")),
        ("/limit time=30", QuickCommand("limit", "time=30")),
        ("/queue edit 2", QuickCommand("queue", "edit 2")),
        ("/steer revise this", QuickCommand("steer", "revise this")),
        ("/steer", QuickCommand("steer")),
        ("/models", QuickCommand("models")),
        ("/models -a openai/*", QuickCommand("models", "-a openai/*")),
        ("/agics", QuickCommand("agics")),
        ("/flows", QuickCommand("flows")),
        ("/review", QuickCommand("review")),
        ("/help unexpected", QuickCommand("help", "unexpected")),
        (":?", RunOverrideHelp()),
    ],
)
def test_parse_single_chat_interaction(source: str, expected: object) -> None:
    assert parse_chat_input(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("/", ""),
        ("/unknown value", "unknown"),
        ("/help\nInput", "help"),
        ("//help", None),
        (":model effort=high", None),
    ],
)
def test_structural_slash_name_is_available_before_body_validation(
    source: str,
    expected: str | None,
) -> None:
    assert slash_command_name(source) == expected


def test_colon_override_without_runnable_input_is_invalid() -> None:
    with pytest.raises(ValueError, match="colon override requires runnable input"):
        parse_chat_input(":model openai/gpt-5\n\n:limit time=30")


def test_policy_and_primary_input_return_one_runnable_branch() -> None:
    assert parse_chat_input(
        ":model openai/gpt-5\n:agic review focus=security -\nReview this"
    ) == (
        RunOverride(
            model=ModelOverride(identity="openai/gpt-5"),
            runnable="agic:review",
        ),
        RunnableInputRaw(
            _="Review this",
            named=(NamedInputSource("focus", "security"),),
        ),
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (":agic review -- Review this", "Review this"),
        (":agic review -\nReview\nthis", "Review\nthis"),
        (":agic review ---\nReview\nthis\n---", "Review\nthis\n"),
        (":agic review -", ""),
        (":agic review ---\n---", ""),
    ],
)
def test_runnable_override_supports_every_call_input_form(
    source: str,
    expected: str,
) -> None:
    assert parse_chat_input(source) == (
        RunOverride(runnable="agic:review"),
        RunnableInputRaw(_=expected),
    )


@pytest.mark.parametrize(
    "source",
    [
        "$review focus=security -- Review this",
        "$review focus=security -\nReview this",
        "$review focus=security ---\nReview this\n---",
    ],
)
def test_chat_submission_preserves_prompt_call_input_for_content_resolution(
    source: str,
) -> None:
    assert parse_chat_input(source) == (
        RunOverride(),
        RunnableInputRaw(_=source),
    )


def test_runnable_override_input_can_contain_prompt_calls() -> None:
    source = ":agic review ---\nBefore\n$wrap -- target\nAfter\n---"
    parsed = parse_chat_input(source)

    assert isinstance(parsed, tuple)
    override, runnable_input = parsed
    assert isinstance(override, RunOverride)
    assert isinstance(runnable_input, RunnableInputRaw)
    assert override == RunOverride(runnable="agic:review")
    assert runnable_input._ is not None
    assert resolve_input_parts(
        runnable_input._,
        program=Program.from_source("prompt wrap:\n  <{{_}}>\n"),
    ) == (TextPart("Before\n<target>\nAfter\n"),)


def test_runnable_named_inputs_make_a_run_without_primary_input() -> None:
    assert parse_chat_input(":flow research topic=agents") == (
        RunOverride(runnable="flow:research"),
        RunnableInputRaw(named=(NamedInputSource("topic", "agents"),)),
    )


def test_limit_override_and_input_return_one_aggregate_override() -> None:
    assert parse_chat_input(":limit tokens=200 time=30\n\nRun") == (
        RunOverride(
            limits=(
                LimitOverride("tokens", 200),
                LimitOverride("time", 30),
            )
        ),
        RunnableInputRaw(_="Run"),
    )


def test_chat_normalization_preserves_first_indentation_and_internal_blanks() -> None:
    source = "\n \t\n  first\n\nsecond  \t\n \n"

    assert normalize_chat_input(source) == "  first\n\nsecond"
    assert parse_chat_input(source) == (
        RunOverride(),
        RunnableInputRaw(_="  first\n\nsecond"),
    )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "empty"),
        (" \t\n", "empty"),
        (":help", "escape a leading colon"),
        (":model", "requires"),
        (":models", "escape a leading colon"),
        (":queue edit 2", "escape a leading colon"),
        (":unknown", "escape a leading colon"),
        ("/help\nInput", "cannot be combined"),
        (":model openai/one\n:model openai/two", "duplicate model override"),
        (":agic review\n/help", "cannot be combined"),
        (":agic review -- Review\nmore", "cannot be followed"),
        (":agic review ---\nReview", "Unclosed fenced input"),
        (":agic review ---\n---\nmore", "cannot be followed"),
    ],
)
def test_invalid_chat_input_is_rejected(source: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_chat_input(source)
