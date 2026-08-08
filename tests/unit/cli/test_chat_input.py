from __future__ import annotations

import pytest

from toolang.cli.toolang.commands.chat.input import (
    QuickCommand,
    normalize_chat_input,
    parse_chat_input,
)
from toolang.execution.types import PolicyCommand
from toolang.lang.input import RunnableInput


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (":help", QuickCommand("help")),
        (":show run_1", QuickCommand("show", "run_1")),
        (":model", QuickCommand("model")),
        (":models", QuickCommand("models")),
        (":agic", QuickCommand("agic")),
        (":flow", QuickCommand("flow")),
        (":runnable", QuickCommand("runnable")),
        (":queue edit 2", QuickCommand("queue", "edit 2")),
        (":steer revise this", QuickCommand("steer", "revise this")),
    ],
)
def test_parse_single_quick_command(source: str, expected: QuickCommand) -> None:
    assert parse_chat_input(source) == expected


def test_policy_only_input_returns_commands() -> None:
    assert parse_chat_input(":model openai/gpt-5\n\n:limit time=30") == (
        PolicyCommand("default", "model", "openai/gpt-5"),
        PolicyCommand("limit", "time", 30),
    )


def test_policy_and_primary_input_return_one_runnable_branch() -> None:
    assert parse_chat_input(
        ":model openai/gpt-5\n\n:agic review focus=security\n\nReview this"
    ) == (
        (
            PolicyCommand("default", "model", "openai/gpt-5"),
            PolicyCommand("default", "runnable", "agic:review"),
        ),
        RunnableInput(
            primary="Review this",
            named=(("focus", "security"),),
        ),
    )


def test_runnable_named_inputs_make_a_run_without_primary_input() -> None:
    assert parse_chat_input(":flow research topic=agents") == (
        (PolicyCommand("default", "runnable", "flow:research"),),
        RunnableInput(named=(("topic", "agents"),)),
    )


def test_allow_shortcuts_are_policy_commands_not_quick_commands() -> None:
    assert parse_chat_input(":models openai/* deepseek/*") == (
        PolicyCommand("allow", "models", ("openai/*", "deepseek/*")),
    )
    assert parse_chat_input(":skills reviewer") == (
        PolicyCommand("allow", "skills", ("reviewer",)),
    )


def test_chat_normalization_preserves_first_indentation_and_internal_blanks() -> None:
    source = "\n \t\n  first\n\nsecond  \t\n \n"

    assert normalize_chat_input(source) == "  first\n\nsecond"
    assert parse_chat_input(source) == (
        (),
        RunnableInput(primary="  first\n\nsecond"),
    )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "empty"),
        (" \t\n", "empty"),
        (":unknown", "unknown command"),
        (":steer", "requires an argument"),
        (":help unexpected", "does not accept an argument"),
        (":help\nInput", "cannot be combined"),
        (":model one\n:model two", "duplicate default field"),
        (":agic review\n:help", "cannot be combined"),
    ],
)
def test_invalid_chat_input_is_rejected(source: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_chat_input(source)
