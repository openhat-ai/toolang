from __future__ import annotations

import pytest

from toolang.cli.toolang.commands.chat.input import (
    QuickCommand,
    normalize_chat_input,
    parse_chat_input,
)
from toolang.execution.types import ModelOverride, LimitOverride, RunOverride
from toolang.lang.input import NamedInputSource, RunnableInputRaw


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
        ("/allow models=openai/*", QuickCommand("allow", "models=openai/*")),
        ("/limit time=30", QuickCommand("limit", "time=30")),
        ("/queue edit 2", QuickCommand("queue", "edit 2")),
        ("/steer revise this", QuickCommand("steer", "revise this")),
    ],
)
def test_parse_single_quick_command(source: str, expected: QuickCommand) -> None:
    assert parse_chat_input(source) == expected


def test_colon_override_without_runnable_input_is_invalid() -> None:
    with pytest.raises(ValueError, match="colon override requires runnable input"):
        parse_chat_input(":model openai/gpt-5\n\n:limit time=30")


def test_policy_and_primary_input_return_one_runnable_branch() -> None:
    assert parse_chat_input(
        ":model openai/gpt-5\n\n:agic review focus=security\n\nReview this"
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
        ("/models", "unknown command"),
        ("/review", "unknown command"),
        (":help", "escape a leading colon"),
        (":model", "requires"),
        (":models", "escape a leading colon"),
        (":queue edit 2", "escape a leading colon"),
        (":unknown", "escape a leading colon"),
        ("/steer", "requires an argument"),
        ("/help unexpected", "does not accept an argument"),
        ("/help\nInput", "cannot be combined"),
        (":model openai/one\n:model openai/two", "duplicate model override"),
        (":agic review\n/help", "cannot be combined"),
    ],
)
def test_invalid_chat_input_is_rejected(source: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_chat_input(source)
