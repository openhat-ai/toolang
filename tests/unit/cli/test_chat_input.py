from __future__ import annotations

import pytest

from toolang.cli.toolang.commands.chat.input import (
    QuickCommand,
    normalize_chat_input,
    parse_chat_input,
)
from toolang.execution.types import RunOverride
from toolang.lang.input import RunnableInputRaw


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("/help", QuickCommand("help")),
        ("/show run_1", QuickCommand("show", "run_1")),
        ("/model", QuickCommand("model")),
        ("/model openai/gpt-5", QuickCommand("model", "openai/gpt-5")),
        ("/agic", QuickCommand("agic")),
        ("/flow research", QuickCommand("flow", "research")),
        ("/runnable", QuickCommand("runnable")),
        ("/queue edit 2", QuickCommand("queue", "edit 2")),
        ("/steer revise this", QuickCommand("steer", "revise this")),
    ],
)
def test_parse_single_quick_command(source: str, expected: QuickCommand) -> None:
    assert parse_chat_input(source) == expected


def test_policy_only_input_returns_commands() -> None:
    assert parse_chat_input(":model openai/gpt-5\n\n:limit time=30") == (
        RunOverride("default", "model", "openai/gpt-5"),
        RunOverride("limit", "time", 30),
    )


def test_policy_and_primary_input_return_one_runnable_branch() -> None:
    assert parse_chat_input(
        ":model openai/gpt-5\n\n:agic review focus=security\n\nReview this"
    ) == (
        (
            RunOverride("default", "model", "openai/gpt-5"),
            RunOverride("default", "runnable", "agic:review"),
        ),
        RunnableInputRaw(
            primary="Review this",
            named=(("focus", "security"),),
        ),
    )


def test_runnable_named_inputs_make_a_run_without_primary_input() -> None:
    assert parse_chat_input(":flow research topic=agents") == (
        (RunOverride("default", "runnable", "flow:research"),),
        RunnableInputRaw(named=(("topic", "agents"),)),
    )


def test_allow_shortcuts_are_run_overrides_not_quick_commands() -> None:
    assert parse_chat_input(":models openai/* deepseek/*") == (
        RunOverride("allow", "models", ("openai/*", "deepseek/*")),
    )
    assert parse_chat_input(":skills reviewer") == (
        RunOverride("allow", "skills", ("reviewer",)),
    )


def test_chat_normalization_preserves_first_indentation_and_internal_blanks() -> None:
    source = "\n \t\n  first\n\nsecond  \t\n \n"

    assert normalize_chat_input(source) == "  first\n\nsecond"
    assert parse_chat_input(source) == (
        (),
        RunnableInputRaw(primary="  first\n\nsecond"),
    )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "empty"),
        (" \t\n", "empty"),
        ("/models", "unknown command"),
        ("/review", "unknown command"),
        (":help", "escape a leading colon"),
        (":model", "escape a leading colon"),
        (":models", "escape a leading colon"),
        (":queue edit 2", "escape a leading colon"),
        (":unknown", "escape a leading colon"),
        ("/steer", "requires an argument"),
        ("/help unexpected", "does not accept an argument"),
        ("/help\nInput", "cannot be combined"),
        (":model one\n:model two", "duplicate default field"),
        (":agic review\n/help", "cannot be combined"),
    ],
)
def test_invalid_chat_input_is_rejected(source: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_chat_input(source)
