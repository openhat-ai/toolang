from __future__ import annotations

from tests.support.setup import materialized_setup

from pathlib import Path


from toolang.base.types.model import (
    ModelRequest,
    Reasoning,
)
from toolang.base.types.policy import AgentCeiling, RunBindings, RunDefaults, RunLimits
from toolang.common.layout import AgentLayout
from toolang.execution.policy import (
    apply_session_setting,
    materialize_run_setting,
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
from toolang.setup import AgentSetup, ToolCollection


def _setup() -> AgentSetup:
    return materialized_setup(
        layout=AgentLayout.resident(Path("/tmp/toolang"), "alice"),
        providers={},
        adapters={},
        models=(),
        tools=ToolCollection(),
        envs={},
        defaults=RunDefaults(model=ModelRequest("root/model")),
        limits=RunLimits(tokens=100, cost=5.0, time=60),
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
            reasoning=Reasoning(effort="high"),
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
        reasoning=Reasoning(effort="low"),
    )
    assert identity_only.model == ModelRequest("anthropic/claude-sonnet-4.5")


def test_effort_budget_auto_default_and_unset_materialize_canonically() -> None:
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
        RunOverride(model=ModelOverride(identity="unset")),
    )

    assert budget.model == ModelRequest(
        "openai/gpt-5",
        reasoning=Reasoning(budget_tokens=4096),
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
    assert limits == RunLimits(tokens=80, cost=5.0, time=None)
