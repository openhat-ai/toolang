from __future__ import annotations

from decimal import Decimal

from toolang.base.types.model import (
    ModelParameters,
    ModelRequest,
    ReasoningParameters,
)
from toolang.base.types.policy import AgentCeiling, RunLimits, RunPolicy
from toolang.cli.toolang.commands.chat.policy import (
    build_run_request,
    run_override_error,
    update_session_setting,
)
from toolang.execution.schemas import RunRequest, RunnableRequest
from toolang.execution.types import (
    AllowOverride,
    LimitOverride,
    ModelOverride,
    RunOverride,
    SessionSetting,
)
from toolang.lang.input import NamedInputSource, RunnableInputRaw


def _surface() -> SessionSetting:
    return SessionSetting(
        model=ModelRequest("openai/gpt-4.1"),
        runnable="agic:chat",
        limits=RunLimits(cost=Decimal("1.50"), time=30),
    )


def test_build_run_request_materializes_a_session_snapshot_without_mutation() -> None:
    setting = SessionSetting(
        model=ModelRequest(
            "openai/gpt-5",
            ModelParameters(ReasoningParameters(effort="high")),
        ),
        runnable="agic:chat",
        allow=AgentCeiling(models=("openai/*",)),
        limits=RunLimits(cost=Decimal("1.50"), time=60),
    )

    request = build_run_request(
        thread_id="term_test",
        request_id="term_request",
        input=RunnableInputRaw(
            _="hello",
            named=(NamedInputSource("tone", "brief"),),
        ),
        override=RunOverride(
            runnable="flow:review",
            allow=(AllowOverride("tools", ("shell/*",)),),
            limits=(LimitOverride("tokens", 2000),),
        ),
        setting=setting,
        surface=_surface(),
        resolve_model_ref=lambda value: value,
        resolve_runnable_ref=lambda value: value,
    )

    assert request == RunRequest(
        thread_id="term_test",
        request_id="term_request",
        runnable=RunnableRequest(
            "flow:review",
            RunnableInputRaw(
                _="hello",
                named=(NamedInputSource("tone", "brief"),),
            ),
        ),
        model=ModelRequest(
            "openai/gpt-5",
            ModelParameters(ReasoningParameters(effort="high")),
        ),
        policy=RunPolicy(
            allow=(
                AgentCeiling(models=("openai/*",)),
                AgentCeiling(tools=("shell/*",)),
            ),
            limits=RunLimits(
                tokens=2000,
                cost=Decimal("1.50"),
                time=60,
            ),
        ),
    )


def test_model_identity_change_clears_unmentioned_parameters() -> None:
    surface = _surface()
    setting = SessionSetting(
        model=ModelRequest(
            "openai/gpt-5",
            ModelParameters(ReasoningParameters(effort="high")),
        ),
        runnable=surface.runnable,
        limits=surface.limits,
    )

    changed = update_session_setting(
        surface=surface,
        current=setting,
        update=RunOverride(model=ModelOverride(identity="anthropic/claude-sonnet-4.5")),
    )

    assert changed.model == ModelRequest("anthropic/claude-sonnet-4.5")


def test_input_local_effort_change_reuses_session_model_identity() -> None:
    request = build_run_request(
        thread_id="term_test",
        request_id="term_request",
        input=RunnableInputRaw(_="hello"),
        override=RunOverride(model=ModelOverride(effort="low")),
        setting=SessionSetting(
            model=ModelRequest(
                "openai/gpt-5",
                ModelParameters(ReasoningParameters(effort="high")),
            ),
            runnable="agic:chat",
            limits=RunLimits(),
        ),
        surface=_surface(),
        resolve_model_ref=lambda value: value,
        resolve_runnable_ref=lambda value: value,
    )

    assert request.model == ModelRequest(
        "openai/gpt-5",
        ModelParameters(ReasoningParameters(effort="low")),
    )


def test_input_local_unset_removes_only_the_run_model() -> None:
    setting = SessionSetting(
        model=ModelRequest("openai/gpt-5"),
        runnable="agic:chat",
        limits=RunLimits(),
    )

    request = build_run_request(
        thread_id="term_test",
        request_id="term_request",
        input=RunnableInputRaw(_="hello"),
        override=RunOverride(
            model=ModelOverride(identity="unset"),
            runnable="flow:review",
        ),
        setting=setting,
        surface=_surface(),
        resolve_model_ref=lambda value: value,
        resolve_runnable_ref=lambda value: value,
    )

    assert request.model is None
    assert setting.model == ModelRequest("openai/gpt-5")


def test_build_run_request_materializes_unqualified_runnable_to_exact_ref() -> None:
    request = build_run_request(
        thread_id="term_test",
        request_id="term_request",
        input=RunnableInputRaw(_="hello"),
        override=RunOverride(runnable="review"),
        setting=_surface(),
        surface=_surface(),
        resolve_model_ref=lambda value: value,
        resolve_runnable_ref=lambda value: {"review": "flow:review"}[value],
    )

    assert request.runnable.ref == "flow:review"


def test_run_override_errors_add_contextual_help_guidance() -> None:
    assert run_override_error(":", "missing") == (
        "Enter a run override after : · See :? for help"
    )
    assert run_override_error(":unknown value", "invalid") == (
        "Unknown run override :unknown · See :? for help"
    )
    assert run_override_error(
        ":model effort=high", "colon override requires runnable input"
    ) == ("Add runnable input after the override · See :? for help")
    assert run_override_error(":model effort=extreme\nhello", "invalid effort.") == (
        "invalid effort · See :? for help"
    )
