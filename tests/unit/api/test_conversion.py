"""Strict authored-run HTTP conversion."""

from decimal import Decimal

from fastapi import HTTPException
import pytest
from pydantic import ValidationError

from toolang.api.conversion import (
    parse_authored_rerun,
    parse_authored_retry,
    parse_authored_run,
)
from toolang.api.schemas import (
    AuthoredRerunRequest,
    AuthoredRetryRequest,
    AuthoredRunRequest,
)
from toolang.base.types.model import (
    ModelParameters,
    ModelRequest,
    ReasoningParameters,
)
from toolang.base.types.policy import AgentCeiling, RunLimits, RunPolicy
from toolang.execution.schemas import (
    RerunRequest,
    RetryRequest,
    RunRequest,
    RunnableRequest,
)
from toolang.execution.types import RunOverride, StepPath
from toolang.lang.input import NamedInputSource, RunnableInputRaw


def test_parse_authored_run_round_trips_every_request_field() -> None:
    payload = AuthoredRunRequest.model_validate(
        {
            "thread_id": "term_example",
            "request_id": "term_request",
            "runnable": {
                "ref": "agic:chat",
                "input": {
                    "_": "hello",
                    "named": [
                        {"name": "tone", "source": "brief"},
                        {"name": "audience", "source": "maintainers"},
                    ],
                },
            },
            "model": {
                "ref": "openai/test",
                "parameters": {"reasoning": {"effort": "high"}},
            },
            "policy": {
                "allow": [{"models": ["one", "two"]}, {"tools": None}],
                "limits": {"tokens": 4000, "cost": "2.50"},
            },
        }
    )

    assert parse_authored_run(payload) == RunRequest(
        thread_id="term_example",
        request_id="term_request",
        runnable=RunnableRequest(
            "agic:chat",
            RunnableInputRaw(
                _="hello",
                named=(
                    NamedInputSource("tone", "brief"),
                    NamedInputSource("audience", "maintainers"),
                ),
            ),
        ),
        model=ModelRequest(
            "openai/test",
            ModelParameters(ReasoningParameters("high")),
        ),
        policy=RunPolicy(
            allow=(
                AgentCeiling(models=("one", "two")),
                AgentCeiling(tools=None),
            ),
            limits=RunLimits(tokens=4000, cost=Decimal("2.50")),
        ),
    )


def test_parse_authored_restart_round_trips_strict_wire_values() -> None:
    retry_payload = AuthoredRetryRequest.model_validate(
        {
            "request_id": "retry_request",
            "commands": [{"group": "limit", "field": "cost", "value": "2.50"}],
            "anchor": "run_source.1.2",
        }
    )
    rerun_payload = AuthoredRerunRequest.model_validate(
        {
            "request_id": "rerun_request",
            "commands": [
                {"group": "default", "field": "model", "value": "openai/test"}
            ],
        }
    )

    assert parse_authored_retry("run_source", retry_payload) == RetryRequest(
        source="run_source",
        commands=(RunOverride("limit", "cost", Decimal("2.50")),),
        request_id="retry_request",
        anchor=StepPath("run_source", (1, 2)),
    )
    assert parse_authored_rerun("run_source", rerun_payload) == RerunRequest(
        source="run_source",
        commands=(RunOverride("default", "model", "openai/test"),),
        request_id="rerun_request",
    )

    with pytest.raises(ValidationError):
        AuthoredRetryRequest.model_validate(
            {"request_id": "retry_request", "unexpected": True}
        )
    with pytest.raises(ValidationError):
        AuthoredRetryRequest.model_validate(
            {
                "request_id": "retry_request",
                "model": {"ref": "openai/test", "parameters": {}},
            }
        )
    with pytest.raises(HTTPException, match="cannot replace the persisted runnable"):
        parse_authored_rerun(
            "run_source",
            AuthoredRerunRequest.model_validate(
                {
                    "request_id": "rerun_request",
                    "commands": [
                        {
                            "group": "default",
                            "field": "runnable",
                            "value": "agic:other",
                        }
                    ],
                }
            ),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"extra": True},
        {"thread_id": 1},
        {"runnable": {"ref": "agic:chat", "input": {"primary": "legacy"}}},
        {"model": {"ref": "openai/test", "parameters": {"temperature": 1}}},
        {"policy": {"allow": [], "limits": {"tokens": -1}}},
    ],
)
def test_authored_run_schema_rejects_extra_or_lossy_values(
    change: dict[str, object],
) -> None:
    source: dict[str, object] = {
        "thread_id": "term_example",
        "request_id": "term_request",
        "runnable": {"ref": "agic:chat", "input": {"_": None, "named": []}},
        "model": None,
        "policy": {"allow": [], "limits": {}},
    }
    source.update(change)

    with pytest.raises(ValidationError):
        AuthoredRunRequest.model_validate(source)
