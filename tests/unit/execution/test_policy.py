from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.common.layout import AgentLayout
from toolang.execution.policy import (
    merge_commands,
    parse_run_override,
    parse_policy_prefix,
    resolve_commands,
)
from toolang.execution.types import RunOverride
from toolang.setup import AgentSetup


def _setup() -> AgentSetup:
    return AgentSetup(
        layout=AgentLayout.resident(Path("/tmp/toolang"), "alice"),
        providers={},
        adapters={},
        models=(),
        tools={},
        envs={},
        ceiling=AgentCeiling(models=("root/*",)),
        bindings=RunBindings(model="root/model", runnable="agic:chat"),
        limits=RunLimits(tokens=100, cost=Decimal("5"), time=60),
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            ":allow models=openai/*,deepseek/*",
            RunOverride("allow", "models", ("openai/*", "deepseek/*")),
        ),
        (":allow tools=none", RunOverride("allow", "tools", ())),
        (":allow caps=all", RunOverride("allow", "caps", None)),
        (":skills all", RunOverride("allow", "skills", None)),
        (
            ":default model=openai/gpt-5",
            RunOverride("default", "model", "openai/gpt-5"),
        ),
        (
            ":default runnable=flow:research",
            RunOverride("default", "runnable", "flow:research"),
        ),
        (":limit tokens=200", RunOverride("limit", "tokens", 200)),
        (":limit cost=1.25", RunOverride("limit", "cost", Decimal("1.25"))),
        (":limit time=none", RunOverride("limit", "time", None)),
        (":model openai/gpt-5", RunOverride("default", "model", "openai/gpt-5")),
        (":agic review", RunOverride("default", "runnable", "agic:review")),
        (":flow research", RunOverride("default", "runnable", "flow:research")),
        (":runnable custom", RunOverride("default", "runnable", "custom")),
    ],
)
def test_parse_run_override_forms(
    source: str,
    expected: RunOverride,
) -> None:
    command, named = parse_run_override(source)

    assert command == expected
    assert named == ()


def test_runnable_shortcut_returns_named_input_sources() -> None:
    command, named = parse_run_override(':agic review focus="security review" count=2')

    assert command == RunOverride("default", "runnable", "agic:review")
    assert named == (("focus", "security review"), ("count", "2"))


def test_parse_prefix_allows_structural_blank_lines() -> None:
    commands, named, primary = parse_policy_prefix(
        ":model openai/gpt-5\n\n:agic review focus=security\n\n\n  Review this."
    )

    assert commands == (
        RunOverride("default", "model", "openai/gpt-5"),
        RunOverride("default", "runnable", "agic:review"),
    )
    assert named == (("focus", "security"),)
    assert primary == "  Review this."


def test_repeated_allow_accumulates_but_scalar_fields_are_unique() -> None:
    commands, _named, _primary = parse_policy_prefix(
        ":models openai/*\n:models deepseek/*\nInput"
    )
    assert commands == (
        RunOverride(
            "allow",
            "models",
            ("openai/*", "deepseek/*"),
        ),
    )

    with pytest.raises(ValueError, match="duplicate default field"):
        parse_policy_prefix(":model one\n:model two\nInput")
    with pytest.raises(ValueError, match="cannot combine selectors with all"):
        parse_policy_prefix(":models one\n:models all\nInput")


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (":allow models", "field=value"),
        (":allow unknown=value", "unknown allow field"),
        (":default other=value", "unknown default field"),
        (":limit other=1", "unknown run limit"),
        (":limit tokens=-1", "non-negative"),
        (":models all none", "cannot mix"),
        (":skills skill/reviewer", "must not include a family"),
        (":agic review focus", "name=value"),
    ],
)
def test_invalid_run_overrides_are_rejected(source: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_run_override(source)


def test_merge_commands_compacts_session_state_and_preserves_disabled_limit() -> None:
    merged = merge_commands(
        (
            RunOverride("default", "model", "old/model"),
            RunOverride("allow", "tools", ("filesystem/*",)),
            RunOverride("limit", "time", 30),
        ),
        (
            RunOverride("default", "model", None),
            RunOverride("allow", "tools", None),
            RunOverride("limit", "time", None),
        ),
    )

    assert merged == (RunOverride("limit", "time", None),)


def test_resolve_commands_overlays_bindings_limits_and_ceilings() -> None:
    ceilings, bindings, limits = resolve_commands(
        _setup(),
        surface=RunBindings(runnable="flow:surface"),
        session=(
            RunOverride("default", "model", "session/model"),
            RunOverride("default", "runnable", "agic:session"),
            RunOverride("limit", "tokens", 80),
            RunOverride("allow", "models", ("session/*",)),
        ),
        run=(
            RunOverride("default", "runnable", None),
            RunOverride("limit", "time", None),
            RunOverride("allow", "models", ("run/*",)),
            RunOverride("allow", "skills", ("reviewer",)),
        ),
    )

    assert bindings == RunBindings(
        model="session/model",
        runnable="flow:surface",
    )
    assert limits == RunLimits(tokens=80, cost=Decimal("5"), time=None)
    assert ceilings == (
        AgentCeiling(models=("session/*",)),
        AgentCeiling(models=("run/*",), caps=("skill/reviewer",)),
    )


def test_allow_all_removes_that_field_before_cap_kind_normalization() -> None:
    unrestricted, _bindings, _limits = resolve_commands(
        _setup(),
        run=(RunOverride("allow", "skills", None),),
    )
    ceilings, _bindings, _limits = resolve_commands(
        _setup(),
        run=(
            RunOverride("allow", "caps", None),
            RunOverride("allow", "skills", ("reviewer",)),
        ),
    )

    assert unrestricted == ()
    assert ceilings == (AgentCeiling(caps=("skill/reviewer",)),)
