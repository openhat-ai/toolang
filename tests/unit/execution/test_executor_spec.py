from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from toolang.base.types.policy import RunBindings
from toolang.execution.executor import RunSpec
from toolang.common.layout import AgentLayout
from toolang.setup import AgentSetup


def _setup() -> AgentSetup:
    return AgentSetup(
        layout=AgentLayout.resident(Path("/"), "alice"),
        providers={},
        adapters={},
        models=(),
        tools={},
        envs={},
    )


def _state() -> Any:
    return cast(Any, SimpleNamespace())


def test_run_spec_has_minimal_execution_contract() -> None:
    assert tuple(field.name for field in fields(RunSpec)) == (
        "setup",
        "state",
        "thread",
        "bindings",
        "limits",
        "ceilings",
        "input",
    )


def test_run_spec_defaults_are_immutable() -> None:
    first_setup = _setup()
    first = RunSpec(
        setup=first_setup,
        state=_state(),
        thread="term_first",
        bindings=RunBindings(runnable="chat"),
        limits=first_setup.limits,
    )
    second_setup = _setup()
    second = RunSpec(
        setup=second_setup,
        state=_state(),
        thread="term_second",
        bindings=RunBindings(runnable="chat"),
        limits=second_setup.limits,
    )

    assert first.input.primary is None
    assert first.input.named == {}
    assert second.input.named == {}
