from __future__ import annotations

from decimal import Decimal

from toolang.base.types.model import (
    ModelParameters,
    ModelRequest,
    ReasoningParameters,
)
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits, RunPolicy
from toolang.cli.toolang.commands.chat.policy import (
    ChatRunDefaults,
    apply_session_commands,
    build_run_request,
)
from toolang.execution.schemas import RunRequest, RunnableRequest
from toolang.execution.types import RunOverride
from toolang.lang.input import NamedInputSource, RunnableInputRaw


def test_build_run_request_materializes_a_session_snapshot_without_mutation() -> None:
    selects: dict[str, object] = {
        "model": "openai/gpt-5",
        "reasoning_effort": "high",
        "run_overrides": (
            RunOverride("default", "model", "openai/gpt-5"),
            RunOverride("allow", "models", ("openai/*",)),
            RunOverride("limit", "time", 60),
        ),
    }
    before = dict(selects)

    request = build_run_request(
        thread_id="term_test",
        request_id="term_request",
        input=RunnableInputRaw(
            _="hello",
            named=(NamedInputSource("tone", "brief"),),
        ),
        input_commands=(
            RunOverride("default", "runnable", "flow:review"),
            RunOverride("allow", "tools", ("shell/*",)),
            RunOverride("limit", "tokens", 2000),
        ),
        selects=selects,
        defaults=ChatRunDefaults(
            bindings=RunBindings(
                model="openai/gpt-4.1",
                runnable="agic:chat",
            ),
            limits=RunLimits(cost=Decimal("1.50"), time=30),
        ),
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
            ModelParameters(ReasoningParameters("high")),
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
    assert selects == before


def test_standalone_model_change_resets_reasoning_to_auto() -> None:
    selected = {
        "model": "openai/gpt-5",
        "reasoning_effort": "high",
    }

    changed = apply_session_commands(
        selected,
        (RunOverride("default", "model", "anthropic/claude-sonnet-4.5"),),
    )

    assert changed["model"] == "anthropic/claude-sonnet-4.5"
    assert "reasoning_effort" not in changed


def test_input_local_model_replacement_does_not_reuse_session_reasoning() -> None:
    request = build_run_request(
        thread_id="term_test",
        request_id="term_request",
        input=RunnableInputRaw(_="hello"),
        input_commands=(
            RunOverride("default", "model", "anthropic/claude-sonnet-4.5"),
        ),
        selects={
            "model": "openai/gpt-5",
            "reasoning_effort": "high",
        },
        defaults=ChatRunDefaults(
            bindings=RunBindings(
                model="openai/gpt-4.1",
                runnable="agic:chat",
            ),
            limits=RunLimits(),
        ),
    )

    assert request.model == ModelRequest("anthropic/claude-sonnet-4.5")
