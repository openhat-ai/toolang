"""Retry only transport failures with a plausible temporary cause."""

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
from openai import APIConnectionError, APIStatusError
import pytest

from toolang.base.errors import ModelResponseError
from toolang.plugin.adapters._errors import model_transport_errors


@pytest.mark.parametrize("sdk", [False, True])
@pytest.mark.parametrize(
    "status", [400, 401, 403, 404, 408, 409, 422, 429, 500, 502, 503, 504]
)
def test_http_failure_classification(status: int, sdk: bool) -> None:
    response = httpx.Response(
        status,
        headers={"Retry-After": "4"},
        request=httpx.Request("POST", "https://example.invalid"),
    )
    error = (
        APIStatusError("failed", response=response, body=None)
        if sdk
        else httpx.HTTPStatusError(
            "failed", request=response.request, response=response
        )
    )
    recoverable = status in {408, 409, 429, 500, 502, 503, 504}
    with pytest.raises(ModelResponseError if recoverable else type(error)) as caught:
        with model_transport_errors():
            raise error
    if recoverable:
        assert isinstance(caught.value, ModelResponseError)
        assert caught.value.kind == "transport_error"
        assert caught.value.retry_after == 4
    else:
        assert caught.value is error


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("timeout"),
        httpx.ConnectError("offline"),
        httpx.RemoteProtocolError("early EOF"),
        APIConnectionError(request=httpx.Request("POST", "https://example.invalid")),
    ],
)
def test_connection_failures_are_recoverable(error: Exception) -> None:
    with pytest.raises(ModelResponseError) as caught:
        with model_transport_errors():
            raise error
    assert caught.value.recoverable
    assert caught.value.usage is None


def test_quota_exhaustion_is_not_retried() -> None:
    response = httpx.Response(
        429, request=httpx.Request("POST", "https://example.invalid")
    )
    error = APIStatusError(
        "no quota", response=response, body={"code": "insufficient_quota"}
    )
    with pytest.raises(APIStatusError) as caught:
        with model_transport_errors():
            raise error
    assert caught.value is error


def test_cancellation_is_not_normalized() -> None:
    with pytest.raises(asyncio.CancelledError):
        with model_transport_errors():
            raise asyncio.CancelledError()


@pytest.mark.parametrize("header", ["invalid", "nan", "inf", "-3", "date"])
def test_retry_after_is_bounded_to_valid_delays(header: str) -> None:
    value = (
        format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10))
        if header == "date"
        else header
    )
    response = httpx.Response(
        429,
        headers={"Retry-After": value},
        request=httpx.Request("POST", "https://example.invalid"),
    )
    with pytest.raises(ModelResponseError) as caught:
        with model_transport_errors():
            response.raise_for_status()
    delay = caught.value.retry_after
    if header == "date":
        assert delay is not None and 8 <= delay <= 10
    else:
        assert delay == (0 if header == "-3" else None)


def test_unread_streaming_http_error_remains_recoverable() -> None:
    response = httpx.Response(
        503,
        stream=httpx.ByteStream(b"temporary"),
        request=httpx.Request("POST", "https://example.invalid"),
    )
    with pytest.raises(ModelResponseError) as caught:
        with model_transport_errors():
            response.raise_for_status()
    assert caught.value.kind == "transport_error"


@pytest.mark.parametrize("permanent", ["quota", "header"])
def test_explicit_http_retry_rejection_is_respected(permanent: str) -> None:
    response = httpx.Response(
        429,
        json={"error": {"code": "insufficient_quota"}} if permanent == "quota" else {},
        headers={"x-should-retry": "false"} if permanent == "header" else {},
        request=httpx.Request("POST", "https://example.invalid"),
    )
    with pytest.raises(httpx.HTTPStatusError):
        with model_transport_errors():
            response.raise_for_status()
