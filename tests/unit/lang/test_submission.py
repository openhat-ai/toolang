from __future__ import annotations

import ast
import inspect
from sys import stdlib_module_names

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


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (":?", QuickCommand(name="?")),
        (":model", QuickCommand(name="model")),
        (":models", QuickCommand(name="models")),
        (":agic", QuickCommand(name="agic")),
        (":flow", QuickCommand(name="flow")),
        (":quit", QuickCommand(name="quit")),
        (":exit", QuickCommand(name="exit")),
        (":show", QuickCommand(name="show")),
        (":show\trun_1", QuickCommand(name="show", tail="run_1")),
        (":queue cancel", QuickCommand(name="queue", tail="cancel")),
        (
            ":steer revise the answer",
            QuickCommand(name="steer", tail="revise the answer"),
        ),
    ],
)
def test_quick_command_forms(source: str, expected: QuickCommand) -> None:
    assert parse_submission(source) == expected


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


@pytest.mark.parametrize(
    ("line", "setting", "override"),
    [
        (
            ":model auto",
            SettingCommand(kind="model", selector="auto"),
            RunOverride(kind="model", selector="auto"),
        ),
        (
            ":model\topenai/gpt-5",
            SettingCommand(kind="model", selector="openai/gpt-5"),
            RunOverride(kind="model", selector="openai/gpt-5"),
        ),
        (
            ":agic auto",
            SettingCommand(kind="agic", selector="auto"),
            RunOverride(kind="agic", selector="auto"),
        ),
        (
            ':agic review focus="security review"',
            SettingCommand(
                kind="agic",
                selector="review",
                args=(("focus", "security review"),),
            ),
            RunOverride(
                kind="agic",
                selector="review",
                args=(("focus", "security review"),),
            ),
        ),
        (
            ":flow pipeline focus=security",
            SettingCommand(
                kind="flow",
                selector="pipeline",
                args=(("focus", "security"),),
            ),
            RunOverride(
                kind="flow",
                selector="pipeline",
                args=(("focus", "security"),),
            ),
        ),
    ],
)
def test_setting_and_override_lines_have_independent_results(
    line: str,
    setting: SettingCommand,
    override: RunOverride,
) -> None:
    assert parse_submission(line) == (setting,)
    assert parse_submission(f"{line}\nInput") == RunnableCall(
        overrides=(override,),
        content="Input",
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
    ("source", "expected"),
    [
        ("Review this.\n", "Review this."),
        ("Review this.\r\n", "Review this."),
        ("Review this.\n\n", "Review this.\n"),
        ("Review this.\r\n\r\n", "Review this.\r\n"),
        ("Review this.\r", "Review this.\r"),
    ],
)
def test_only_one_terminal_line_break_is_discarded(
    source: str,
    expected: str,
) -> None:
    assert parse_runnable_call(source).content == expected


@pytest.mark.parametrize("line_break", ["\n", "\r\n"])
def test_line_break_forms_apply_to_quick_settings_and_calls(
    line_break: str,
) -> None:
    assert parse_submission(f":help{line_break}") == QuickCommand(name="help")
    assert parse_submission(
        f":model auto{line_break}{line_break}"
    ) == (SettingCommand(kind="model", selector="auto"),)
    assert parse_submission(
        f":agic review{line_break}Input{line_break}"
    ) == RunnableCall(
        overrides=(RunOverride(kind="agic", selector="review"),),
        content="Input",
    )


def test_command_prefix_is_special_only_before_content_starts() -> None:
    assert parse_runnable_call("Review this\n:help").content == (
        "Review this\n:help"
    )
    assert parse_runnable_call(":agic review\n::help").content == "::help"

    with pytest.raises(ValueError, match="escape a leading colon"):
        parse_submission("\n:help")


def test_setting_commands_allow_trailing_blank_lines() -> None:
    assert parse_submission(":model auto\n \t\n\n") == (
        SettingCommand(kind="model", selector="auto"),
    )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "submission is empty"),
        (" \t\n", "input content is empty"),
        (":unknown", "unknown command"),
        (":steer", "requires an argument"),
        (":help unexpected", "does not accept an argument"),
        (':model "', "No closing quotation"),
        (':model ""', "selector is empty"),
        (":model openai/gpt-5 focus=x", ":model accepts no arguments"),
        (":flow auto", ":flow auto is not supported"),
        (":agic auto focus=x", ":agic auto accepts no arguments"),
        (":agic review focus", "name=value syntax"),
        (":agic review 1focus=x", "name=value syntax"),
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


def test_run_only_parser_rejects_quick_and_treats_selector_lines_as_overrides() -> None:
    with pytest.raises(ValueError, match="not a runnable call"):
        parse_runnable_call(":help")
    assert parse_runnable_call(":model openai/gpt-5") == RunnableCall(
        overrides=(RunOverride(kind="model", selector="openai/gpt-5"),),
        content="",
    )


def test_run_only_parser_accepts_an_absent_optional_input() -> None:
    assert parse_runnable_call("") == RunnableCall(overrides=(), content="")
    assert parse_runnable_call("\r\n") == RunnableCall(overrides=(), content="")


def test_submission_module_has_no_toolang_imports() -> None:
    tree = ast.parse(inspect.getsource(submission_module))
    imports: set[str] = set()
    relative_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative_imports.append("." * node.level + (node.module or ""))
            else:
                imports.add(node.module or "")

    external_imports = {
        name
        for name in imports
        if name.split(".", maxsplit=1)[0] not in stdlib_module_names
    }
    assert relative_imports == []
    assert external_imports == set()
