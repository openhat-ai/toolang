from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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
        "runnable",
        "input",
        "model",
        "args",
    )


def test_run_spec_defaults_are_immutable() -> None:
    first = RunSpec(
        setup=_setup(), state=_state(), thread="term_first", runnable="chat"
    )
    second = RunSpec(
        setup=_setup(), state=_state(), thread="term_second", runnable="chat"
    )

    assert first.input == ()
    assert first.args is None
    assert second.args is None
