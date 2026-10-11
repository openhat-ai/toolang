from __future__ import annotations

import pytest

from toolang.base.types.model import ModelOverride
from toolang.execution.input import (
    parse_call,
    parse_run_prefix,
    parse_slash_directive,
    parse_setting_override,
)
from toolang.execution.types import AllowOverride, LimitOverride, RunOverride
from toolang.lang.input import CallInput


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "/model openai/gpt-5",
            RunOverride(model=ModelOverride(identity="openai/gpt-5")),
        ),
        (
            "/model effort=high",
            RunOverride(model=ModelOverride(effort="high")),
        ),
        (
            "/model openai/gpt-5 effort=4096",
            RunOverride(model=ModelOverride(identity="openai/gpt-5", effort=4096)),
        ),
        (
            "/model effort=auto",
            RunOverride(model=ModelOverride(effort="auto")),
        ),
        ("/model unset", RunOverride(model=ModelOverride(identity="unset"))),
        ("/agic:review", RunOverride(runnable="agic:review")),
        ("/flow:research", RunOverride(runnable="flow:research")),
        ("/custom", RunOverride(runnable="custom")),
        ("/workdir repo://src", RunOverride(workdir="repo://src")),
        (
            "/limit tokens=200 cost=1.25 time=none",
            RunOverride(
                limits=(
                    LimitOverride("tokens", 200),
                    LimitOverride("cost", 1.25),
                    LimitOverride("time", None),
                )
            ),
        ),
    ],
)
def test_parse_slash_directive_forms(source: str, expected: RunOverride) -> None:
    override, named = parse_slash_directive(source)

    assert override == expected
    assert named == {}


def test_root_runnable_call_returns_named_input_sources() -> None:
    override, named = parse_slash_directive(
        '/agic:review focus="security review" count=2'
    )

    assert override == RunOverride(runnable="agic:review")
    assert named == {"focus": "security review", "count": "2"}


def test_prefix_merges_allow_lines_and_multiple_fields() -> None:
    override, call_input = parse_run_prefix(
        "/allow models=openai/* tools=shell/*\n"
        "/allow models=deepseek/* skills=reviewer\n\nRun this."
    )

    assert override == RunOverride(
        allow=(
            AllowOverride("models", ("openai/*", "deepseek/*")),
            AllowOverride("tools", ("shell/*",)),
            AllowOverride("skills", ("reviewer",)),
        )
    )
    assert call_input == CallInput({"_": "Run this."})


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("/model", "requires"),
        ("/model openai/gpt-5 high", "first token"),
        ("/model ref=openai/gpt-5", "unknown model parameter"),
        ("/model reasoning=high", "unknown model parameter"),
        ("/model none", "was removed"),
        ("/allow", "requires"),
        ("/allow unknown=value", "unknown allow field"),
        ("/limit tokens=-1", "non-negative"),
        ("/limit tokens=1 tokens=2", "duplicate limit field"),
        (":default model=openai/gpt-5", "not a slash directive"),
        (":models openai/*", "not a slash directive"),
    ],
)
def test_invalid_or_removed_override_forms_are_rejected(
    source: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_slash_directive(source)


def test_slash_and_colon_share_the_same_setting_body_parser() -> None:
    override, _named = parse_slash_directive("/model openai/gpt-5 effort=high")

    assert parse_setting_override("model", "openai/gpt-5 effort=high") == override
    assert parse_setting_override(
        "allow", "models=openai/* tools=shell/*"
    ) == RunOverride(
        allow=(
            AllowOverride("models", ("openai/*",)),
            AllowOverride("tools", ("shell/*",)),
        )
    )


@pytest.mark.parametrize(
    ("sentinel", "value"),
    [("all", None), ("none", ())],
)
def test_repeated_allow_sentinels_are_idempotent(
    sentinel: str,
    value: tuple[str, ...] | None,
) -> None:
    expected = RunOverride(allow=(AllowOverride("models", value),))

    assert (
        parse_setting_override(
            "allow",
            f"models={sentinel} models={sentinel}",
        )
        == expected
    )
    override, call_input = parse_run_prefix(
        f"/allow models={sentinel}\n/allow models={sentinel}\n\nRun"
    )
    assert override == expected
    assert call_input == CallInput({"_": "Run"})


@pytest.mark.parametrize("sentinel", ["all", "none"])
def test_allow_sentinels_still_reject_a_query_for_the_same_field(
    sentinel: str,
) -> None:
    with pytest.raises(ValueError, match="cannot combine"):
        parse_setting_override(
            "allow",
            f"models={sentinel} models=openai/*",
        )


def test_parse_call_rejects_an_override_without_runnable_input() -> None:
    with pytest.raises(
        ValueError, match="run override prefix requires primary or named input"
    ):
        parse_call("/model effort=high")


def test_parse_call_allows_an_empty_call_without_an_override() -> None:
    assert parse_call("") == (RunOverride(), CallInput())


@pytest.mark.parametrize(
    ("source", "runnable"),
    [
        ("/agic:review", "agic:review"),
        ("/agic:research", "agic:research"),
        ("/flow:research", "flow:research"),
        ("/research", "research"),
        ("/default", "default"),
    ],
)
@pytest.mark.parametrize("prefix", ["", "/model effort=high\n"])
def test_parse_call_allows_empty_input_for_explicit_runnable(
    source: str, runnable: str, prefix: str
) -> None:
    if prefix:
        with pytest.raises(ValueError, match="requires primary or named input"):
            parse_call(prefix + source)
        return
    assert parse_call(prefix + source) == (
        RunOverride(
            runnable=runnable,
        ),
        CallInput(),
    )
