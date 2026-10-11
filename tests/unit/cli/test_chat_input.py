from __future__ import annotations

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.message import TextPart
from toolang.cli.toolang.commands.chat.input import (
    ChatCommand,
    command_name,
    normalize_chat_input,
    parse_chat_input,
    run_directive_error,
)
from toolang.execution.input import COLON_COMMAND_NAMES, SLASH_COMMAND_NAMES, parse_call
from toolang.execution.types import ModelOverride, RunOverride
from toolang.lang.ast import Program
from toolang.lang.input import CallInput, resolve_input_parts, resolve_runnable_input


@pytest.mark.parametrize("name", sorted(SLASH_COMMAND_NAMES))
def test_slash_commands_are_standalone(name: str) -> None:
    assert parse_chat_input(f"/{name}") == ChatCommand(name)
    with pytest.raises(ValueError, match="cannot be combined"):
        parse_chat_input(f"/{name}\nInput")
    with pytest.raises(ValueError, match="standalone Chat command"):
        parse_call(f"/{name}")


@pytest.mark.parametrize("name", sorted(COLON_COMMAND_NAMES))
def test_colon_commands_are_session_interactions(name: str) -> None:
    assert parse_chat_input(f":{name}") == ChatCommand(name, prefix=":")
    with pytest.raises(ValueError, match="cannot be combined"):
        parse_chat_input(f":{name}\nInput")
    with pytest.raises(ValueError, match="colon commands"):
        parse_call(f":{name}")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("/", None),
        ("/unknown value", None),
        ("/review", None),
        ("/model effort=high", None),
        ("/help\nInput", "help"),
        ("//help", None),
        (":model effort=high", "model"),
        (":unknown", "unknown"),
        ("::model", None),
    ],
)
def test_command_classification_excludes_run_directives(source, expected) -> None:
    assert command_name(source) == expected


@pytest.mark.parametrize("parse", [parse_chat_input, parse_call])
@pytest.mark.parametrize(
    "ref",
    [
        "review",
        "agic:review",
        "flow:review",
        "module::flow:review",
        "runnable",
        "agic",
        "flow",
        "default",
        "cd",
    ],
)
def test_direct_runnable_call_can_have_absent_input(parse, ref) -> None:
    assert parse(f"/{ref}") == (RunOverride(runnable=ref), CallInput())
    with pytest.raises(ValueError, match="requires primary or named input"):
        parse(f"/model effort=high\n/{ref}")
    assert parse(f"/model effort=high\n/{ref} topic=security") == (
        RunOverride(runnable=ref, model=ModelOverride(effort="high")),
        CallInput({"topic": "security"}),
    )


@pytest.mark.parametrize("parse", [parse_chat_input, parse_call])
@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (" -- Review this", "Review this"),
        (" -\nReview\nthis", "Review\nthis"),
        ("\nReview\nthis", "Review\nthis"),
        (" -", ""),
    ],
)
def test_root_input_forms_and_named_arguments(parse, tail, expected) -> None:
    assert parse('/review topic="security review"' + tail) == (
        RunOverride(runnable="review"),
        CallInput({"topic": "security review", "_": expected}),
    )


@pytest.mark.parametrize("parse", [parse_chat_input, parse_call])
@pytest.mark.parametrize(
    "following", ["more", "$summarize -- text", "/help", "/model effort=high"]
)
def test_root_line_input_rejects_following_content(parse, following) -> None:
    with pytest.raises(ValueError, match="cannot be followed"):
        parse(f"/review -- text\n{following}")
    assert parse("/review -- text\n \t\n") == (
        RunOverride(runnable="review"),
        CallInput({"_": "text"}),
    )


@pytest.mark.parametrize("parse", [parse_chat_input, parse_call])
@pytest.mark.parametrize("body", ["", "\n---", "\ntext\n---\nmore"])
def test_fenced_root_input_is_rejected(parse, body) -> None:
    with pytest.raises(ValueError, match="only supported for prompt calls"):
        parse("/review ---" + body)


def test_fenced_prompt_returns_to_root_content() -> None:
    source = "/review -\n$summarize ---\nSource material.\n---\nAlso check security."
    override, input = parse_call(source)
    assert override.runnable == "review"
    assert resolve_input_parts(
        input["_"], program=Program.from_source("prompt summarize:\n  Summary: {{_}}\n")
    ) == (TextPart("Summary: Source material.\nAlso check security."),)


@pytest.mark.parametrize("parse", [parse_chat_input, parse_call])
@pytest.mark.parametrize("marker", ["", " -"])
def test_root_header_ends_directive_prefix_parsing(parse, marker) -> None:
    assert parse(f"/review{marker}\n/model effort=high\n/help\n---") == (
        RunOverride(runnable="review"),
        CallInput({"_": "/model effort=high\n/help\n---"}),
    )


@pytest.mark.parametrize("parse", [parse_chat_input, parse_call])
@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("/ review", "immediately after"),
        ("/", "immediately after"),
        ("/runnable review", "name=value"),
        ("/agic review", "name=value"),
        ("/flow review", "name=value"),
        ("/review --", "nonempty"),
        ("/review - text", "must end"),
        ("/review topic=a topic=b", "duplicate"),
        ("/review _=text", "primary input"),
        ("/model effort=high", "requires primary or named input"),
        ("/model effort=high\n/limit time=30", "requires primary or named input"),
        ("/model effort=high\n/help", "standalone Chat command"),
        ("/model effort=high\n:workdir src", "colon commands"),
        ("/model openai/one\n/model openai/two\nText", "duplicate model override"),
    ],
)
def test_invalid_run_submissions_fail_atomically(parse, source, message) -> None:
    with pytest.raises(ValueError, match=message):
        parse(source)


@pytest.mark.parametrize("name", ["help", "models", "unknown", "cd"])
def test_unknown_colon_commands_reject(name) -> None:
    with pytest.raises(ValueError, match="unknown colon command"):
        parse_chat_input(f":{name}")


@pytest.mark.parametrize("parse", [parse_chat_input, parse_call])
def test_builtin_argument_errors_do_not_fall_back_to_runnables(parse) -> None:
    with pytest.raises(ValueError, match="unknown model parameter"):
        parse("/model topic=security")
    assert parse("/flow:model topic=security") == (
        RunOverride(runnable="flow:model"),
        CallInput({"topic": "security"}),
    )


@pytest.mark.parametrize(
    "source",
    [
        "//help",
        "::model literal",
        "$$summarize",
        "@@README.md",
        "  /review",
        "```\n/review\n```",
    ],
)
def test_literal_input_is_not_dispatched(source) -> None:
    assert parse_chat_input(source) == (RunOverride(), CallInput({"_": source}))


def test_chat_normalization_preserves_indentation_and_internal_blanks() -> None:
    source = "\n \t\n  first\n\nsecond  \t\n \n"
    assert normalize_chat_input(source) == "  first\n\nsecond"
    assert parse_chat_input(source) == (
        RunOverride(),
        CallInput({"_": "  first\n\nsecond"}),
    )


@pytest.mark.parametrize("source", ["", " \t\n"])
def test_empty_chat_submission_rejects(source) -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_chat_input(source)


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize(
    ("signature", "tail", "expected_keys"),
    [
        ("", " -- text", {"_"}),
        ("()", "", set()),
        ("(_)", " -", {"_"}),
        ("(_: Text)", " -- text", {"_"}),
        ("(topic)", " topic=security", {"topic"}),
        ("(topic?)", "", set()),
        (
            "(topic, count: Number, detail?)",
            " count=0 topic=security",
            {"topic", "count"},
        ),
        ("(_, topic, detail?)", " topic=security -- text", {"_", "topic"}),
        ("(_, detail?)", " -- text", {"_"}),
        (
            "(topic?: Text, enabled?: Boolean)",
            ' topic="" enabled=false',
            {"topic", "enabled"},
        ),
    ],
)
def test_signature_matrix_binds_identically_for_agics_and_flows(
    kind, signature, tail, expected_keys
) -> None:
    program = Program.from_source(f"{kind} r{signature}:\n  pass\n")
    runnable = (program.agics if kind == "agic" else program.flows)[0]
    for prefix in ("", "/model effort=high\n"):
        if prefix and not expected_keys:
            with pytest.raises(ValueError, match="requires primary or named input"):
                parse_call(prefix + "/r" + tail)
            continue
        _, input = parse_call(prefix + "/r" + tail)
        resolved = resolve_runnable_input(
            runnable,
            {name: resolve_input_parts(value) for name, value in input.items()},
        )
        assert set(resolved) == expected_keys


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize(
    ("signature", "tail", "message"),
    [
        ("", "", "requires primary input"),
        ("()", " -", "does not accept primary input"),
        ("(_)", "", "requires primary input"),
        ("(topic)", "", "missing named inputs"),
        ("(_, topic)", " topic=security", "requires primary input"),
        ("(_, topic)", " -- text", "missing named inputs"),
        ("(topic?)", " unknown=value", "unknown named inputs"),
        ("(count: Number)", " count=nope", "Number"),
    ],
)
def test_signature_rejections_follow_parsing(kind, signature, tail, message) -> None:
    program = Program.from_source(f"{kind} r{signature}:\n  pass\n")
    runnable = (program.agics if kind == "agic" else program.flows)[0]
    _, input = parse_call("/r" + tail)
    with pytest.raises((ValueError, TypeError, ToolangError), match=message):
        resolve_runnable_input(
            runnable,
            {name: resolve_input_parts(value) for name, value in input.items()},
        )


def test_run_directive_errors_add_contextual_help_guidance() -> None:
    assert run_directive_error("/", "missing") == (
        "Enter a directive name immediately after / · See /help for help"
    )
    assert run_directive_error("/unknown value", "invalid") == (
        "invalid · See /help for help"
    )
    assert run_directive_error(
        "/model effort=high", "run override prefix requires primary or named input"
    ) == ("Include primary or named input after run overrides · See /help for help")
    assert run_directive_error("/model effort=extreme\nhello", "invalid effort.") == (
        "invalid effort · See /help for help"
    )
