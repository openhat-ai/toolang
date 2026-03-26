from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

RunOrigin = Literal["invoke", "chat", "task", "chore", "will"]
RunType = Literal["turn", "model", "tool", "agent", "system"]
AgentChangeType = Literal[
    "caps_updated",
    "code_updated",
    "config_updated",
    "task_updated",
    "chore_updated",
    "will_updated",
]

RUN_TYPES: set[str] = {"turn", "model", "tool", "agent", "system"}
EVENT_TYPES: set[str] = {
    "agent_created",
    "agent_removed",
    "agent_started",
    "agent_stopped",
    "caps_updated",
    "code_updated",
    "config_updated",
    "task_updated",
    "chore_updated",
    "will_updated",
    "run_started",
    "run_finished",
    "run_failed",
}


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True, slots=True)
class AgentStarted:
    at: str
    agent_uri: str
    agent_id: str
    name: str
    kind: str
    sandbox: str
    endpoint: str
    agent_home: str
    source_file: str

    @property
    def event_type(self) -> str:
        return "agent_started"


@dataclass(frozen=True, slots=True)
class AgentCreated:
    at: str
    agent_uri: str
    agent_id: str
    name: str
    kind: str
    detail: str
    agent_home: str | None = None
    source_file: str | None = None

    @property
    def event_type(self) -> str:
        return "agent_created"


@dataclass(frozen=True, slots=True)
class AgentRemoved:
    at: str
    agent_uri: str
    agent_id: str
    name: str
    kind: str
    detail: str
    agent_home: str | None = None
    source_file: str | None = None

    @property
    def event_type(self) -> str:
        return "agent_removed"


@dataclass(frozen=True, slots=True)
class AgentStopped:
    at: str
    agent_uri: str
    agent_id: str
    name: str
    sandbox: str
    detail: str
    endpoint: str | None = None
    agent_home: str | None = None
    source_file: str | None = None

    @property
    def event_type(self) -> str:
        return "agent_stopped"


@dataclass(frozen=True, slots=True)
class AgentChanged:
    at: str
    agent_uri: str
    agent_id: str
    name: str
    change_type: AgentChangeType
    detail: str
    agent_home: str | None = None
    source_file: str | None = None

    @property
    def event_type(self) -> str:
        return self.change_type


@dataclass(frozen=True, slots=True)
class RunStarted:
    at: str
    agent_uri: str
    agent_id: str
    run_id: str
    run_type: RunType
    origin: RunOrigin
    summary: str
    thunk_name: str | None
    parent_run_id: str | None = None
    thread_id: str | None = None

    @property
    def event_type(self) -> str:
        return "run_started"


@dataclass(frozen=True, slots=True)
class RunFinished:
    at: str
    agent_uri: str
    agent_id: str
    run_id: str
    run_type: RunType
    origin: RunOrigin
    summary: str
    thunk_name: str | None
    parent_run_id: str | None = None
    thread_id: str | None = None

    @property
    def event_type(self) -> str:
        return "run_finished"


@dataclass(frozen=True, slots=True)
class RunFailed:
    at: str
    agent_uri: str
    agent_id: str
    run_id: str
    run_type: RunType
    origin: RunOrigin
    error: str
    thunk_name: str | None
    parent_run_id: str | None = None
    thread_id: str | None = None

    @property
    def event_type(self) -> str:
        return "run_failed"


AgentEvent = AgentCreated | AgentRemoved | AgentStarted | AgentStopped | AgentChanged
RunEvent = RunStarted | RunFinished | RunFailed
BusEvent = AgentEvent | RunEvent


def serialize_event(event: BusEvent) -> dict[str, Any]:
    if isinstance(event, AgentCreated):
        payload: dict[str, Any] = {
            "name": event.name,
            "kind": event.kind,
            "detail": event.detail,
        }
        if event.agent_home is not None:
            payload["agent_home"] = event.agent_home
        if event.source_file is not None:
            payload["source_file"] = event.source_file
        return _record(
            event_type=event.event_type,
            at=event.at,
            agent_uri=event.agent_uri,
            agent_id=event.agent_id,
            run_id=None,
            payload=payload,
        )

    if isinstance(event, AgentRemoved):
        payload = {
            "name": event.name,
            "kind": event.kind,
            "detail": event.detail,
        }
        if event.agent_home is not None:
            payload["agent_home"] = event.agent_home
        if event.source_file is not None:
            payload["source_file"] = event.source_file
        return _record(
            event_type=event.event_type,
            at=event.at,
            agent_uri=event.agent_uri,
            agent_id=event.agent_id,
            run_id=None,
            payload=payload,
        )

    if isinstance(event, AgentStarted):
        payload: dict[str, Any] = {
            "name": event.name,
            "kind": event.kind,
            "sandbox": event.sandbox,
            "endpoint": event.endpoint,
            "agent_home": event.agent_home,
            "source_file": event.source_file,
        }
        return _record(
            event_type=event.event_type,
            at=event.at,
            agent_uri=event.agent_uri,
            agent_id=event.agent_id,
            run_id=None,
            payload=payload,
        )

    if isinstance(event, AgentStopped):
        payload = {"name": event.name, "detail": event.detail, "sandbox": event.sandbox}
        if event.endpoint is not None:
            payload["endpoint"] = event.endpoint
        if event.agent_home is not None:
            payload["agent_home"] = event.agent_home
        if event.source_file is not None:
            payload["source_file"] = event.source_file
        return _record(
            event_type=event.event_type,
            at=event.at,
            agent_uri=event.agent_uri,
            agent_id=event.agent_id,
            run_id=None,
            payload=payload,
        )

    if isinstance(event, AgentChanged):
        payload = {
            "name": event.name,
            "detail": event.detail,
        }
        if event.agent_home is not None:
            payload["agent_home"] = event.agent_home
        if event.source_file is not None:
            payload["source_file"] = event.source_file
        return _record(
            event_type=event.event_type,
            at=event.at,
            agent_uri=event.agent_uri,
            agent_id=event.agent_id,
            run_id=None,
            payload=payload,
        )

    if isinstance(event, RunStarted):
        return _record(
            event_type=event.event_type,
            at=event.at,
            agent_uri=event.agent_uri,
            agent_id=event.agent_id,
            run_id=event.run_id,
            payload=_run_payload(event),
        )

    if isinstance(event, RunFinished):
        return _record(
            event_type=event.event_type,
            at=event.at,
            agent_uri=event.agent_uri,
            agent_id=event.agent_id,
            run_id=event.run_id,
            payload=_run_payload(event),
        )

    return _record(
        event_type=event.event_type,
        at=event.at,
        agent_uri=event.agent_uri,
        agent_id=event.agent_id,
        run_id=event.run_id,
        payload=_run_payload(event),
    )


def _run_payload(event: RunStarted | RunFinished | RunFailed) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_type": event.run_type,
        "origin": event.origin,
        "thunk_name": event.thunk_name,
        "parent_run_id": event.parent_run_id,
        "thread_id": event.thread_id,
    }
    if isinstance(event, RunStarted | RunFinished):
        payload["summary"] = event.summary
    if isinstance(event, RunFailed):
        payload["error"] = event.error
    return payload


def _record(
    *,
    event_type: str,
    at: str,
    agent_uri: str,
    agent_id: str,
    run_id: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "at": at,
        "agent_uri": agent_uri,
        "agent_id": agent_id,
        "run_id": run_id,
        "payload": payload,
    }
