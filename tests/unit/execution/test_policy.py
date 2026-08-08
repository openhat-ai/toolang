from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.common.layout import AgentLayout
from toolang.execution.policy import (
    merge_commands,
    parse_policy_command,
    parse_policy_prefix,
    resolve_commands,
)
from toolang.execution.types import PolicyCommand
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
            PolicyCommand("allow", "models", ("openai/*", "deepseek/*")),
        ),
        (":allow tools=none", PolicyCommand("allow", "tools", ())),
        (":allow caps=all", PolicyCommand("allow", "caps", None)),
        (":skills all", PolicyCommand("allow", "skills", None)),
        (
            ":default model=openai/gpt-5",
            PolicyCommand("default", "model", "openai/gpt-5"),
        ),
        (
            ":default runnable=flow:research",
            PolicyCommand("default", "runnable", "flow:research"),
        ),
        (":limit tokens=200", PolicyCommand("limit", "tokens", 200)),
        (":limit cost=1.25", PolicyCommand("limit", "cost", Decimal("1.25"))),
        (":limit time=none", PolicyCommand("limit", "time", None)),
        (":model openai/gpt-5", PolicyCommand("default", "model", "openai/gpt-5")),
        (":agic review", PolicyCommand("default", "runnable", "agic:review")),
        (":flow research", PolicyCommand("default", "runnable", "flow:research")),
        (":runnable custom", PolicyCommand("default", "runnable", "custom")),
    ],
)
def test_parse_policy_command_forms(
    source: str,
    expected: PolicyCommand,
) -> None:
    command, named = parse_policy_command(source)

    assert command == expected
    assert named == ()


def test_runnable_shortcut_returns_named_input_sources() -> None:
    command, named = parse_policy_command(
        ':agic review focus="security review" count=2'
    )

    assert command == PolicyCommand("default", "runnable", "agic:review")
    assert named == (("focus", "security review"), ("count", "2"))


def test_parse_prefix_allows_structural_blank_lines() -> None:
    commands, named, primary = parse_policy_prefix(
        ":model openai/gpt-5\n\n"
        ":agic review focus=security\n\n\n"
        "  Review this."
    )

    assert commands == (
        PolicyCommand("default", "model", "openai/gpt-5"),
        PolicyCommand("default", "runnable", "agic:review"),
    )
    assert named == (("focus", "security"),)
    assert primary == "  Review this."


def test_repeated_allow_accumulates_but_scalar_fields_are_unique() -> None:
    commands, _named, _primary = parse_policy_prefix(
        ":models openai/*\n:models deepseek/*\nInput"
    )
    assert commands == (
        PolicyCommand(
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
def test_invalid_policy_commands_are_rejected(source: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_policy_command(source)


def test_merge_commands_compacts_session_state_and_preserves_disabled_limit() -> None:
    merged = merge_commands(
        (
            PolicyCommand("default", "model", "old/model"),
            PolicyCommand("allow", "tools", ("filesystem/*",)),
            PolicyCommand("limit", "time", 30),
        ),
        (
            PolicyCommand("default", "model", None),
            PolicyCommand("allow", "tools", None),
            PolicyCommand("limit", "time", None),
        ),
    )

    assert merged == (PolicyCommand("limit", "time", None),)


def test_resolve_commands_overlays_bindings_limits_and_separate_ceilings() -> None:
    restrictions, bindings, limits = resolve_commands(
        _setup(),
        surface=RunBindings(runnable="flow:surface"),
        session=(
            PolicyCommand("default", "model", "session/model"),
            PolicyCommand("default", "runnable", "agic:session"),
            PolicyCommand("limit", "tokens", 80),
            PolicyCommand("allow", "models", ("session/*",)),
        ),
        run=(
            PolicyCommand("default", "runnable", None),
            PolicyCommand("limit", "time", None),
            PolicyCommand("allow", "models", ("run/*",)),
            PolicyCommand("allow", "skills", ("reviewer",)),
        ),
    )

    assert bindings == RunBindings(
        model="session/model",
        runnable="flow:surface",
    )
    assert limits == RunLimits(tokens=80, cost=Decimal("5"), time=None)
    assert restrictions == (
        AgentCeiling(models=("session/*",)),
        AgentCeiling(models=("run/*",), caps=("skill/reviewer",)),
    )


def test_allow_all_removes_that_field_before_cap_kind_normalization() -> None:
    unrestricted, _bindings, _limits = resolve_commands(
        _setup(),
        run=(PolicyCommand("allow", "skills", None),),
    )
    restrictions, _bindings, _limits = resolve_commands(
        _setup(),
        run=(
            PolicyCommand("allow", "caps", None),
            PolicyCommand("allow", "skills", ("reviewer",)),
        ),
    )

    assert unrestricted == ()
    assert restrictions == (AgentCeiling(caps=("skill/reviewer",)),)
