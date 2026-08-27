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
from toolang.execution.schemas import RerunRequest, RetryRequest, RunRequest
from toolang.execution.types import RunOverride, StepPath
from toolang.lang.input import RunnableInputRaw


def test_parse_authored_run_round_trips_every_request_field() -> None:
    payload = AuthoredRunRequest.model_validate(
        {
            "thread": "term_example",
            "request_id": "term_request",
            "commands": [
                {"group": "allow", "field": "models", "value": ["one", "two"]},
                {"group": "allow", "field": "tools", "value": None},
                {"group": "default", "field": "model", "value": "openai/test"},
                {"group": "default", "field": "runnable", "value": None},
            ],
            "input": {
                "primary": "hello",
                "named": [
                    {"name": "tone", "source": "brief"},
                    {"name": "audience", "source": "maintainers"},
                ],
            },
            "session_commands": [
                {"group": "limit", "field": "tokens", "value": 4000},
                {"group": "limit", "field": "time", "value": None},
                {"group": "limit", "field": "cost", "value": "2.50"},
            ],
            "runnable_fallbacks": ["agic:chat", "default"],
        }
    )

    assert parse_authored_run(payload) == RunRequest(
        thread="term_example",
        request_id="term_request",
        commands=(
            RunOverride("allow", "models", ("one", "two")),
            RunOverride("allow", "tools", None),
            RunOverride("default", "model", "openai/test"),
            RunOverride("default", "runnable", None),
        ),
        input=RunnableInputRaw(
            primary="hello",
            named=(("tone", "brief"), ("audience", "maintainers")),
        ),
        session_commands=(
            RunOverride("limit", "tokens", 4000),
            RunOverride("limit", "time", None),
            RunOverride("limit", "cost", Decimal("2.50")),
        ),
        runnable_fallbacks=("agic:chat", "default"),
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
    ("change", "detail"),
    [
        (
            {"commands": [{"group": "allow", "field": "models", "value": "all"}]},
            "allow policy value must be selectors, all, or none",
        ),
        (
            {"commands": [{"group": "default", "field": "model", "value": 1}]},
            "default policy value must be a string or none",
        ),
        (
            {"commands": [{"group": "limit", "field": "cost", "value": "02.50"}]},
            "limit cost expects canonical non-negative decimal text",
        ),
        (
            {"commands": [{"group": "limit", "field": "tokens", "value": -1}]},
            "integer run limit value must be non-negative",
        ),
        (
            {"commands": [{"group": "limit", "field": "unknown", "value": 1}]},
            "unknown limit field: unknown",
        ),
        (
            {
                "input": {
                    "primary": "hello",
                    "named": [
                        {"name": "tone", "source": "brief"},
                        {"name": "tone", "source": "direct"},
                    ],
                }
            },
            "duplicate named input: tone",
        ),
        (
            {"runnable_fallbacks": ["agic:chat", "agic:chat"]},
            "run request runnable fallbacks must be unique",
        ),
    ],
)
def test_parse_authored_run_rejects_invalid_core_values(
    change: dict[str, object],
    detail: str,
) -> None:
    source: dict[str, object] = {
        "thread": "term_example",
        "request_id": "term_request",
        "input": {"primary": "hello"},
        "runnable_fallbacks": ["agic:chat", "default"],
    }
    source.update(change)
    payload = AuthoredRunRequest.model_validate(source)

    with pytest.raises(HTTPException) as caught:
        parse_authored_run(payload)

    assert caught.value.status_code == 422
    assert caught.value.detail == detail


@pytest.mark.parametrize(
    "change",
    [
        {"extra": True},
        {"thread": 1},
        {"commands": [{"group": "limit", "field": "tokens", "value": True}]},
        {"commands": [{"group": "limit", "field": "cost", "value": 2.5}]},
        {"runnable_fallbacks": []},
    ],
)
def test_authored_run_schema_rejects_extra_or_lossy_values(
    change: dict[str, object],
) -> None:
    source: dict[str, object] = {
        "thread": "term_example",
        "request_id": "term_request",
        "input": {"primary": None},
        "runnable_fallbacks": ["default"],
    }
    source.update(change)

    with pytest.raises(ValidationError):
        AuthoredRunRequest.model_validate(source)
