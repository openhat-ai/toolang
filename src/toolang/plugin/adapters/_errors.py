"""Normalize transient model transport failures for runtime-owned retries."""

from contextlib import contextmanager
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
from functools import wraps
from typing import Any, ParamSpec, TypeVar

import httpx
from openai import APIConnectionError, APIStatusError

from toolang.base.errors import ModelResponseError
from toolang.base.types.run import ModelUsage


def _retry_after(response: httpx.Response) -> float | None:
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


@contextmanager
def model_transport_errors(
    *,
    usage: Callable[[], ModelUsage | None] = lambda: None,
    partial_text: Callable[[], str] = lambda: "",
) -> Iterator[None]:
    """Keep permanent failures unchanged; never catch cancellation."""

    try:
        yield
    except (
        APIConnectionError,
        httpx.TimeoutException,
        httpx.NetworkError,
        httpx.RemoteProtocolError,
    ) as exc:
        raise ModelResponseError(
            f"temporary model connection failure: {type(exc).__name__}",
            kind="transport_error",
            usage=usage(),
            partial_text=partial_text(),
        ) from exc
    except (APIStatusError, httpx.HTTPStatusError) as exc:
        if exc.response.status_code not in {408, 409, 429, 500, 502, 503, 504}:
            raise
        if exc.response.headers.get("x-should-retry") == "false":
            raise
        code = getattr(exc, "code", None)
        try:
            body = exc.response.json()
        except (ValueError, httpx.ResponseNotRead):
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            code = body["error"].get("code", code)
        if code == "insufficient_quota":
            raise
        raise ModelResponseError(
            f"temporary model HTTP failure: {exc.response.status_code}",
            kind="transport_error",
            usage=usage(),
            partial_text=partial_text(),
            retry_after=_retry_after(exc.response),
        ) from exc


_P = ParamSpec("_P")
_T = TypeVar("_T")


def model_transport(
    call: Callable[_P, Awaitable[_T]],
) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """Normalize errors at the adapter boundary without hidden SDK retries."""

    @wraps(call)
    async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        with model_transport_errors():
            return await call(*args, **kwargs)

    return wrapped
