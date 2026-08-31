from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from toolang.base.types.model import (
    ModelParameters,
    ModelRequest,
    ReasoningParameters,
)
from toolang.base.types.policy import AgentCeiling, RunBindings, RunDefaults, RunLimits
from toolang.common.layout import AgentLayout
from toolang.execution.policy import (
    apply_session_setting,
    materialize_run_setting,
    parse_policy_prefix,
    parse_run_override,
    parse_setting_override,
    resolve_commands,
)
from toolang.execution.types import (
    AllowOverride,
    LimitOverride,
    ModelOverride,
    RunCommand,
    RunOverride,
    SessionSetting,
)
from toolang.lang.input import NamedInputSource
from toolang.setup import AgentSetup, ModelCollection, ToolCollection


def _setup() -> AgentSetup:
    return AgentSetup(
        layout=AgentLayout.resident(Path("/tmp/toolang"), "alice"),
        providers={},
        adapters={},
        models=ModelCollection(),
        tools=ToolCollection(),
        envs={},
        defaults=RunDefaults(model="root/model", runnable="agic:chat"),
        limits=RunLimits(tokens=100, cost=Decimal("5"), time=60),
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            ":model openai/gpt-5",
            RunOverride(model=ModelOverride(identity="openai/gpt-5")),
        ),
        (
            ":model effort=high",
            RunOverride(model=ModelOverride(effort="high")),
        ),
        (
            ":model openai/gpt-5 effort=4096",
            RunOverride(model=ModelOverride(identity="openai/gpt-5", effort=4096)),
        ),
        (
            ":model effort=auto",
            RunOverride(model=ModelOverride(effort="auto")),
        ),
        (":agic review", RunOverride(runnable="agic:review")),
        (":flow research", RunOverride(runnable="flow:research")),
        (":runnable custom", RunOverride(runnable="custom")),
        (
            ":limit tokens=200 cost=1.25 time=none",
            RunOverride(
                limits=(
                    LimitOverride("tokens", 200),
                    LimitOverride("cost", Decimal("1.25")),
                    LimitOverride("time", None),
                )
            ),
        ),
    ],
)
def test_parse_run_override_forms(source: str, expected: RunOverride) -> None:
    override, named = parse_run_override(source)

    assert override == expected
    assert named == ()


def test_runnable_override_returns_named_input_sources() -> None:
    override, named = parse_run_override(':agic review focus="security review" count=2')

    assert override == RunOverride(runnable="agic:review")
    assert named == (
        NamedInputSource("focus", "security review"),
        NamedInputSource("count", "2"),
    )


def test_prefix_merges_allow_lines_and_multiple_fields() -> None:
    override, named, primary = parse_policy_prefix(
        ":allow models=openai/* tools=shell/*\n"
        ":allow models=deepseek/* skills=reviewer\n\nRun this."
    )

    assert override == RunOverride(
        allow=(
            AllowOverride("models", ("openai/*", "deepseek/*")),
            AllowOverride("tools", ("shell/*",)),
            AllowOverride("skills", ("reviewer",)),
        )
    )
    assert named == ()
    assert primary == "Run this."


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (":model", "requires"),
        (":model openai/gpt-5 high", "first token"),
        (":model ref=openai/gpt-5", "unknown model parameter"),
        (":model reasoning=high", "unknown model parameter"),
        (":model effort=01", "unknown reasoning effort"),
        (":allow", "requires"),
        (":allow unknown=value", "unknown allow field"),
        (":limit tokens=-1", "non-negative"),
        (":limit tokens=1 tokens=2", "duplicate limit field"),
        (":default model=openai/gpt-5", "not a run override"),
        (":models openai/*", "not a run override"),
    ],
)
def test_invalid_or_removed_override_forms_are_rejected(
    source: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_run_override(source)


def test_slash_and_colon_share_the_same_setting_body_parser() -> None:
    colon, _named = parse_run_override(":model openai/gpt-5 effort=high")

    assert parse_setting_override("model", "openai/gpt-5 effort=high") == colon
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
    override, _named, primary = parse_policy_prefix(
        f":allow models={sentinel}\n:allow models={sentinel}\n\nRun"
    )
    assert override == expected
    assert primary == "Run"


@pytest.mark.parametrize("sentinel", ["all", "none"])
def test_allow_sentinels_still_reject_a_query_for_the_same_field(
    sentinel: str,
) -> None:
    with pytest.raises(ValueError, match="cannot combine"):
        parse_setting_override(
            "allow",
            f"models={sentinel} models=openai/*",
        )


def test_model_identity_and_effort_have_independent_update_boundaries() -> None:
    surface = SessionSetting(
        model=ModelRequest("openai/gpt-4.1"),
        runnable="agic:chat",
        limits=RunLimits(),
    )
    current = SessionSetting(
        model=ModelRequest(
            "openai/gpt-5",
            ModelParameters(ReasoningParameters(effort="high")),
        ),
        runnable="agic:chat",
        limits=RunLimits(),
    )

    effort_only = apply_session_setting(
        surface,
        current,
        RunOverride(model=ModelOverride(effort="low")),
    )
    identity_only = apply_session_setting(
        surface,
        current,
        RunOverride(model=ModelOverride(identity="anthropic/claude-sonnet-4.5")),
    )

    assert effort_only.model == ModelRequest(
        "openai/gpt-5",
        ModelParameters(ReasoningParameters(effort="low")),
    )
    assert identity_only.model == ModelRequest("anthropic/claude-sonnet-4.5")


def test_effort_budget_auto_default_and_none_materialize_canonically() -> None:
    surface = SessionSetting(
        model=ModelRequest("openai/gpt-4.1"),
        runnable="agic:chat",
        limits=RunLimits(),
    )
    session = apply_session_setting(
        surface,
        surface,
        RunOverride(model=ModelOverride(identity="openai/gpt-5", effort="high")),
    )

    _ceilings, budget = materialize_run_setting(
        surface,
        session,
        RunOverride(model=ModelOverride(effort=4096)),
    )
    _ceilings, automatic = materialize_run_setting(
        surface,
        session,
        RunOverride(model=ModelOverride(effort="auto")),
    )
    _ceilings, defaulted = materialize_run_setting(
        surface,
        session,
        RunOverride(model=ModelOverride(identity="default")),
    )
    _ceilings, disabled = materialize_run_setting(
        surface,
        session,
        RunOverride(model=ModelOverride(identity="none")),
    )

    assert budget.model == ModelRequest(
        "openai/gpt-5",
        ModelParameters(ReasoningParameters(budget_tokens=4096)),
    )
    assert automatic.model == ModelRequest("openai/gpt-5")
    assert defaulted.model == surface.model
    assert disabled.model is None


def test_allow_and_limits_apply_with_distinct_session_and_run_semantics() -> None:
    surface = SessionSetting(
        model=ModelRequest("openai/gpt-5"),
        runnable="agic:chat",
        limits=RunLimits(tokens=100, time=60),
    )
    session = apply_session_setting(
        surface,
        surface,
        RunOverride(
            allow=(AllowOverride("models", ("openai/*",)),),
            limits=(LimitOverride("tokens", 80),),
        ),
    )
    ceilings, effective = materialize_run_setting(
        surface,
        session,
        RunOverride(
            allow=(AllowOverride("tools", ("shell/*",)),),
            limits=(LimitOverride("time", None),),
        ),
    )

    assert ceilings == (
        AgentCeiling(models=("openai/*",)),
        AgentCeiling(tools=("shell/*",)),
    )
    assert effective.limits == RunLimits(tokens=80, time=None)


def test_retained_execution_commands_still_resolve_without_input_changes() -> None:
    ceilings, bindings, limits = resolve_commands(
        _setup(),
        surface=RunBindings(runnable="flow:surface"),
        session=(
            RunCommand("default", "model", "session/model"),
            RunCommand("limit", "tokens", 80),
            RunCommand("allow", "models", ("session/*",)),
        ),
        run=(RunCommand("limit", "time", None),),
    )

    assert ceilings == (AgentCeiling(models=("session/*",)),)
    assert bindings == RunBindings(model="session/model", runnable="flow:surface")
    assert limits == RunLimits(tokens=80, cost=Decimal("5"), time=None)
