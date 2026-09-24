"""Normalize transient model transport failures for runtime-owned retries."""

from contextlib import contextmanager
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import json
from functools import wraps
from typing import Any, ParamSpec, TypeVar, cast

import httpx
from openai import APIConnectionError, APIError, APIStatusError

from toolang.base.errors import ModelResponseError
from toolang.base.types.run import ModelUsage, ModelPartUpdate, ModelStreamHandler


def _retry_after(response: httpx.Response) -> float | None:
    # Match the SDK's preference for the more precise millisecond header.
    try:
        milliseconds = float(response.headers["retry-after-ms"])
    except (KeyError, ValueError):
        pass
    else:
        if math.isfinite(milliseconds):
            return max(0, milliseconds / 1000)
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (
                parsedate_to_datetime(value) - datetime.now(timezone.utc)
            ).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, delay) if math.isfinite(delay) else None


# Provider error codes, not free-form messages. Unknown errors stay terminal.
_TRANSIENT_CODES = {
    "server_error",
    "internal_error",
    "rate_limit_exceeded",
    "rate_limit_error",
    "overloaded_error",
    "api_error",
    "RESOURCE_EXHAUSTED",
    "UNAVAILABLE",
    "INTERNAL",
    "DEADLINE_EXCEEDED",
}


def provider_error(value: object) -> ModelResponseError:
    """Classify an error carried inside an otherwise successful HTTP response."""

    data = cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}
    code = next(
        (
            data[key]
            for key in ("code", "type", "status")
            if isinstance(data.get(key), str) and data[key]
        ),
        "unknown",
    )
    return ModelResponseError(
        f"provider response error: {code}",
        kind="transport_error" if code in _TRANSIENT_CODES else "provider_rejection",
    )


async def raise_for_model_status(response: httpx.Response) -> None:
    """Read streaming HTTP error bodies before classifying quota failures."""

    if response.is_error:
        try:
            await response.aread()
        except httpx.HTTPError:
            # Headers already establish the failure. A lost error body cannot
            # turn a known authentication rejection into a network retry.
            response.raise_for_status()
    response.raise_for_status()


def _classify(error: Exception) -> ModelResponseError | None:
    if isinstance(error, ModelResponseError):
        return error
    if isinstance(error, json.JSONDecodeError):
        return ModelResponseError(
            "provider response contains incomplete or invalid JSON",
            kind="incomplete_stream",
        )
    if isinstance(
        error,
        (
            APIConnectionError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ),
    ):
        return ModelResponseError(
            f"temporary model connection failure: {type(error).__name__}",
            kind="transport_error",
        )
    if isinstance(error, (APIStatusError, httpx.HTTPStatusError)):
        response = error.response
        if response.status_code not in {408, 409, 429, 500, 502, 503, 504, 529}:
            return None
        if response.headers.get("x-should-retry") == "false":
            return None
        code = getattr(error, "code", None)
        try:
            body = response.json()
        except (ValueError, httpx.ResponseNotRead):
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            code = body["error"].get("code", code)
        if code == "insufficient_quota":
            return None
        return ModelResponseError(
            f"temporary model HTTP failure: {response.status_code}",
            kind="transport_error",
            retry_after=_retry_after(response),
        )
    # The SDK raises plain APIError for in-band streaming failures. Do not
    # reinterpret other subclasses such as response validation failures.
    if type(error) is APIError:
        return provider_error(error.body)
    return None


@contextmanager
def model_transport_errors(
    *,
    usage: Callable[[], ModelUsage | None] = lambda: None,
    partial_text: Callable[[], str] = lambda: "",
) -> Iterator[None]:
    """Normalize known failures and retain response facts; never catch cancellation."""

    try:
        yield
    except (ModelResponseError, APIError, httpx.HTTPError, json.JSONDecodeError) as exc:
        error = _classify(exc)
        if error is None:
            raise
        if error.usage is None:
            error.usage = usage()
        if not error.partial_text:
            error.partial_text = partial_text()
        if error is exc:
            raise
        raise error from exc


class _ModelObserverError(Exception):
    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


def model_events(handler: ModelStreamHandler) -> ModelStreamHandler:
    """Keep observer failures outside provider recovery, even if they use HTTP."""

    async def emit(event: ModelPartUpdate) -> None:
        try:
            await handler(event)
        except Exception as exc:
            raise _ModelObserverError(exc) from exc

    return emit


_P = ParamSpec("_P")
_T = TypeVar("_T")


def model_transport(
    call: Callable[_P, Awaitable[_T]],
) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """Normalize errors at the adapter boundary without hidden SDK retries."""

    @wraps(call)
    async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        try:
            with model_transport_errors():
                return await call(*args, **kwargs)
        except _ModelObserverError as exc:
            raise exc.error from exc

    return wrapped
