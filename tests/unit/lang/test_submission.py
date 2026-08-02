from __future__ import annotations

import ast
import inspect

import pytest

import toolang.lang.submission as submission_module
from toolang.lang.submission import (
    QuickCommand,
    RunnableCall,
    RunOverride,
    SettingCommand,
    parse_runnable_call,
    parse_submission,
)


def test_quick_command_occupies_the_complete_submission() -> None:
    assert parse_submission(":help\n") == QuickCommand(name="help")
    assert parse_submission(":show run_1\n\n") == QuickCommand(
        name="show",
        tail="run_1",
    )

    with pytest.raises(ValueError, match="cannot be combined"):
        parse_submission(":help\nReview this")


def test_setting_commands_are_independent_line_values() -> None:
    assert parse_submission(
        ':model openai/gpt-5\n:agic review focus="security review"\n'
    ) == (
        SettingCommand(kind="model", selector="openai/gpt-5"),
        SettingCommand(
            kind="agic",
            selector="review",
            args=(("focus", "security review"),),
        ),
    )


def test_overrides_and_content_form_one_runnable_call() -> None:
    assert parse_submission(
        ":model openai/gpt-5\r\n"
        ":agic review focus=security\r\n"
        "\r\n"
        "Review this API.\r\n"
        "@./api.md\r\n"
    ) == RunnableCall(
        overrides=(
            RunOverride(kind="model", selector="openai/gpt-5"),
            RunOverride(
                kind="agic",
                selector="review",
                args=(("focus", "security"),),
            ),
        ),
        content="Review this API.\r\n@./api.md",
    )


def test_only_one_blank_separator_line_is_discarded() -> None:
    call = parse_runnable_call(":agic review\n\n\nReview this")

    assert call.content == "\nReview this"


def test_bare_content_preserves_source_after_the_terminal_line_break() -> None:
    assert parse_runnable_call("Review this.\n\n").content == "Review this.\n"
    assert parse_runnable_call("::model literal\n").content == "::model literal"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "submission is empty"),
        (" \t\n", "input content is empty"),
        (":unknown", "unknown command"),
        (":steer", "requires an argument"),
        (":flow auto", ":flow auto is not supported"),
        (":agic auto focus=x", ":agic auto accepts no arguments"),
        (":agic review focus=x focus=y", "duplicate argument"),
        (":model one\n:model two", "duplicate model"),
        (":agic one\n:flow two", "duplicate runnable"),
        (
            ":agic review focus=security -- Review this API.",
            "name=value syntax",
        ),
        (":agic review\n:help", "escape a leading colon"),
    ],
)
def test_invalid_submissions_are_rejected(source: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_submission(source)


def test_run_only_parser_rejects_quick_and_setting_commands() -> None:
    with pytest.raises(ValueError, match="not a runnable call"):
        parse_runnable_call(":help")
    with pytest.raises(ValueError, match="not a runnable call"):
        parse_runnable_call(":model openai/gpt-5")


def test_submission_module_has_no_toolang_imports() -> None:
    tree = ast.parse(inspect.getsource(submission_module))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    assert not {name for name in imported if name.startswith("toolang")}
