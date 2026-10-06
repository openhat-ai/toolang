from tests.support.setup import materialized_setup
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.base.types.policy import RunBindings
from toolang.execution.executor import RunSpec
from toolang.common.layout import AgentLayout
from toolang.setup import AgentSetup, ToolCollection
from toolang.lang.input import CallInput


def _setup() -> AgentSetup:
    return materialized_setup(
        layout=AgentLayout.resident(Path("/"), "alice"),
        providers={},
        adapters={},
        models=(),
        tools=ToolCollection(),
        envs={},
    )


def _state() -> Any:
    return cast(Any, SimpleNamespace())


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

    with pytest.raises(TypeError):
        cast(Any, first.input)["_"] = "changed"
    with pytest.raises(FrozenInstanceError):
        cast(Any, first).input = CallInput({"_": "changed"})

    updated = replace(first, input=CallInput({"_": "changed"}))

    assert updated.input["_"] == "changed"
    assert first.input == {}
    assert second.input == {}
