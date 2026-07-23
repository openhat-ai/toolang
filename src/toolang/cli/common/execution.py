"""Shared remote-or-local execution inspection client."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import click
from pydantic import TypeAdapter
import typer

from toolang.execution.inspection import ExecutionInspection
from toolang.execution.schemas import RunDetail, RunInfo, ThreadDetail, ThreadInfo
from toolang.execution.store import RunStore
from toolang.execution.types import RunStatus
from toolang.up.process import agent_run_store_path
from .context import context_root, require_prefix_agent


RemoteGet = Callable[[typer.Context, str], object]

_RUN_INFOS = TypeAdapter(list[RunInfo])
_THREAD_INFOS = TypeAdapter(list[ThreadInfo])
_RUN_DETAIL = TypeAdapter(RunDetail)
_THREAD_DETAIL = TypeAdapter(ThreadDetail)


class LocalExecutionClient:
    """Adapt the local execution store to the read-only HTTP projection surface."""

    def __init__(self, store: RunStore) -> None:
        self.store = store
        self.inspection = ExecutionInspection(store)

    @classmethod
    def open(cls, ctx: typer.Context) -> LocalExecutionClient | None:
        path = agent_run_store_path(context_root(ctx), require_prefix_agent(ctx))
        return cls(RunStore(path)) if path.exists() else None

    def close(self) -> None:
        self.store.close()

    def get(self, path: str) -> dict[str, Any] | None:
        """Return one local projection for a supported execution API path."""

        parsed = urlsplit(path)
        parts = tuple(item for item in parsed.path.split("/") if item)
        if parts[:2] != ("api", "v1"):
            return None
        query = parse_qs(parsed.query)
        resource = parts[2:]
        if resource == ("threads",):
            items = self.inspection.list_threads(
                limit=_query_int(query, "limit", default=50),
                origin=_query_text(query, "origin"),
                channel=_query_text(query, "channel"),
                status=_query_text(query, "status"),
            )
            return {"items": _THREAD_INFOS.dump_python(items, mode="json")}
        if resource == ("runs",):
            items = self.inspection.list_runs(
                limit=_query_int(query, "limit", default=50),
                thread_id=_query_text(query, "thread_id"),
                status=_run_status(_query_text(query, "status")),
            )
            return {"items": _RUN_INFOS.dump_python(items, mode="json")}
        if len(resource) == 2 and resource[0] == "runs":
            detail = self.inspection.run_detail(resource[1])
            if detail is None:
                raise click.ClickException(f"run not found: {resource[1]}")
            return cast(dict[str, Any], _RUN_DETAIL.dump_python(detail, mode="json"))
        if len(resource) == 2 and resource[0] == "threads":
            detail = self.inspection.thread_detail(
                resource[1], limit=_query_int(query, "limit", default=50)
            )
            if detail is None:
                raise click.ClickException(f"thread not found: {resource[1]}")
            return cast(dict[str, Any], _THREAD_DETAIL.dump_python(detail, mode="json"))
        return None


def execution_get(
    ctx: typer.Context,
    path: str,
    *,
    remote_get: RemoteGet,
) -> dict[str, Any]:
    """Read an execution projection remotely, falling back to the local store."""

    try:
        remote = remote_get(ctx, path)
        if isinstance(remote, list):
            return {"items": remote}
        if isinstance(remote, dict):
            return cast(dict[str, Any], remote)
        raise click.ClickException("runtime returned an unsupported JSON response")
    except click.ClickException as remote_error:
        client = LocalExecutionClient.open(ctx)
        if client is None:
            if urlsplit(path).path in {"/api/v1/runs", "/api/v1/threads"}:
                return {"items": []}
            raise remote_error
        try:
            result = client.get(path)
        finally:
            client.close()
        if result is None:
            raise remote_error
        return result


def _query_text(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key, ())
    return values[0] if values and values[0] else None


def _query_int(query: dict[str, list[str]], key: str, *, default: int) -> int:
    value = _query_text(query, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise click.ClickException(f"invalid {key}: {value}") from exc


def _run_status(value: str | None) -> RunStatus | None:
    if value is None:
        return None
    if value not in {"pending", "running", "finished", "failed", "canceled"}:
        raise click.ClickException(f"unknown run status: {value}")
    return cast(RunStatus, value)
