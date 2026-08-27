"""Convert public HTTP schemas into core runtime values."""

from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from toolang.base.types.message import Message, Part
from toolang.execution.schemas import RerunRequest, RetryRequest, RunRequest
from toolang.execution.types import RunOverride
from toolang.lang.input import parse_input
from .schemas import (
    AuthoredRunRequest,
    AuthoredRunValidationRequest,
    AuthoredRerunRequest,
    AuthoredRetryRequest,
    InputMessagePayload,
    InputPart,
    RunOverridePayload,
)


def parse_authored_run(payload: AuthoredRunRequest) -> RunRequest:
    """Reconstruct one transport-neutral run request from strict HTTP data."""

    try:
        return RunRequest(
            thread=payload.thread,
            commands=tuple(_parse_run_override(item) for item in payload.commands),
            input=parse_input(
                payload.input.primary,
                named=tuple((item.name, item.source) for item in payload.input.named),
            ),
            session_commands=tuple(
                _parse_run_override(item) for item in payload.session_commands
            ),
            runnable_fallbacks=tuple(payload.runnable_fallbacks),
            request_id=payload.request_id,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def parse_authored_run_validation(
    payload: AuthoredRunValidationRequest,
) -> tuple[tuple[RunOverride, ...], tuple[str, ...]]:
    """Reconstruct one strict authored-run session validation request."""

    try:
        commands = tuple(_parse_run_override(item) for item in payload.session_commands)
        fallbacks = tuple(payload.runnable_fallbacks)
        RunRequest(
            thread="term_validation",
            commands=(),
            input=parse_input(None),
            session_commands=commands,
            runnable_fallbacks=fallbacks,
            request_id="term_validation",
        )
        return commands, fallbacks
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def parse_authored_retry(
    source: str,
    payload: AuthoredRetryRequest,
) -> RetryRequest:
    """Reconstruct one transport-neutral retry request from strict HTTP data."""

    try:
        return RetryRequest(
            source=source,
            commands=tuple(_parse_run_override(item) for item in payload.commands),
            request_id=payload.request_id,
            anchor=payload.anchor,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def parse_authored_rerun(
    source: str,
    payload: AuthoredRerunRequest,
) -> RerunRequest:
    """Reconstruct one transport-neutral rerun request from strict HTTP data."""

    try:
        return RerunRequest(
            source=source,
            commands=tuple(_parse_run_override(item) for item in payload.commands),
            request_id=payload.request_id,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def parse_user_message(payload: InputMessagePayload) -> Message:
    """Parse and validate one user-authored API message."""

    try:
        message = Message.from_data(
            payload.model_dump(mode="python", exclude_none=True)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if message.role != "user":
        raise HTTPException(status_code=422, detail="input message role must be user")
    return message


def parse_parts(parts: list[InputPart]) -> tuple[Part, ...]:
    """Parse and validate canonical HTTP input parts."""

    try:
        return Message.from_data(
            {
                "role": "user",
                "parts": [
                    part.model_dump(mode="python", exclude_none=True) for part in parts
                ],
            }
        ).parts
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _parse_run_override(payload: RunOverridePayload) -> RunOverride:
    value = payload.value
    if payload.group == "allow":
        if value is not None and not isinstance(value, list):
            raise TypeError("allow policy value must be selectors, all, or none")
        return RunOverride(
            "allow",
            payload.field,
            tuple(value) if isinstance(value, list) else None,
        )
    if payload.group == "default":
        if value is not None and not isinstance(value, str):
            raise TypeError("default policy value must be a string or none")
        return RunOverride("default", payload.field, value)
    if payload.field == "cost":
        if value is None:
            return RunOverride("limit", payload.field, None)
        if not isinstance(value, str):
            raise TypeError("limit cost policy value must be decimal text or none")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("limit cost expects decimal text or none") from exc
        if not parsed.is_finite() or parsed < 0 or str(parsed) != value:
            raise ValueError("limit cost expects canonical non-negative decimal text")
        return RunOverride("limit", payload.field, parsed)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise TypeError("integer run limit value must be an integer or none")
    if isinstance(value, int) and value < 0:
        raise ValueError("integer run limit value must be non-negative")
    return RunOverride("limit", payload.field, value)
