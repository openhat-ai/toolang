"""Remote AgentServer identity checks shared by CLI execution surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
from typing import cast

import httpx


class RemoteRuntimeError(RuntimeError):
    """One remote AgentServer health, transport, or identity failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RemoteRuntimeIdentity:
    """Validated public identity of one AgentServer runtime."""

    version: str
    driver: str
    selector: str
    instance: str | None
    description: str | None


async def inspect_remote_runtime(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    expected_sandbox: str,
) -> RemoteRuntimeIdentity:
    """Require one healthy AgentServer with the expected sandbox identity."""

    expected = expected_sandbox.strip()
    if not expected or expected != expected_sandbox:
        raise ValueError("remote runtime requires a canonical sandbox identity")
    base = endpoint.rstrip("/")
    health = await _request_json(client, f"{base}/healthz", operation="health")
    if health != {"ok": True}:
        raise RemoteRuntimeError("remote runtime health check returned invalid data")
    profile = await _request_json(
        client,
        f"{base}/api/v1/profile",
        operation="profile",
    )
    try:
        identity = parse_remote_runtime_identity(profile)
    except ValueError as exc:
        raise RemoteRuntimeError(f"remote runtime {exc}") from exc
    if identity.selector != expected:
        raise RemoteRuntimeError(
            "running executor sandbox does not match its runtime status"
        )
    return identity


def parse_remote_runtime_identity(payload: object) -> RemoteRuntimeIdentity:
    """Parse the strict execution identity subset of one agent profile."""

    profile = _mapping(payload, label="profile")
    runtime = _mapping(profile.get("runtime"), label="profile runtime")
    if not {"version", "sandbox"}.issubset(runtime):
        raise ValueError("profile returned invalid runtime identity")
    sandbox = _mapping(runtime.get("sandbox"), label="profile sandbox")
    if not {"driver", "selector", "instance"}.issubset(sandbox):
        raise ValueError("profile returned invalid sandbox identity")
    version = _label(runtime.get("version"), label="runtime version")
    driver = _token(sandbox.get("driver"), label="sandbox driver")
    selector = _label(sandbox.get("selector"), label="sandbox selector")
    if selector.partition(":")[0] != driver:
        raise ValueError("sandbox selector does not match its driver")
    instance_value = sandbox.get("instance")
    description_value = sandbox.get("description")
    if driver == "docker":
        if description_value is not None:
            raise ValueError("docker sandbox returned a description")
        instance = _token(instance_value, label="sandbox instance")
        description = None
    else:
        if instance_value is not None:
            raise ValueError("non-docker sandbox returned an instance ID")
        instance = None
        description = (
            None
            if description_value is None
            else _description(description_value, label="sandbox description")
        )
    return RemoteRuntimeIdentity(version, driver, selector, instance, description)


async def _request_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    operation: str,
) -> object:
    try:
        response = await client.get(url)
    except (httpx.HTTPError, RuntimeError) as exc:
        raise RemoteRuntimeError(
            f"remote runtime {operation} transport failed: {type(exc).__name__}"
        ) from exc
    if not response.is_success:
        detail = response.reason_phrase or "request failed"
        try:
            payload = response.json()
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        else:
            if isinstance(payload, Mapping) and isinstance(payload.get("detail"), str):
                detail = cast(str, payload["detail"])
        raise RemoteRuntimeError(
            f"remote runtime {operation} failed: HTTP {response.status_code} {detail}",
            status_code=response.status_code,
            detail=detail,
        )
    try:
        return response.json()
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteRuntimeError(
            f"remote runtime {operation} returned invalid JSON"
        ) from exc


def _mapping(payload: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} returned invalid data")
    return cast(Mapping[str, object], payload)


def _label(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    text = value.strip()
    if (
        not text
        or text != value
        or not text.isprintable()
        or any(character.isspace() for character in text)
        or any(character in text for character in ",()")
    ):
        raise ValueError(f"{label} is invalid")
    return text


def _description(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    text = value.strip()
    if not text or text != value or not text.isprintable():
        raise ValueError(f"{label} is invalid")
    return text


def _token(value: object, *, label: str) -> str:
    text = _label(value, label=label)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", text) is None:
        raise ValueError(f"{label} is invalid")
    return text
