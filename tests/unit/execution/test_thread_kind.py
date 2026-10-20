from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import fields, replace
import sqlite3

from fastapi.testclient import TestClient
from pydantic import TypeAdapter
import pytest

from tests.support.execution_fixtures import accept_run
from toolang.api.app import create_app
from toolang.base.types.message import Message
from toolang.catalog import CapsManager, ChoresManager
from toolang.common.layout import AgentLayout
from toolang.execution.events import ThreadCreated, event_to_data, event_from_data
from toolang.execution.inspection.history import RunHistory
from toolang.execution.records import ThreadRecord
from toolang.execution.schemas import ThreadInfo, record_to_data
from toolang.execution.store import RunStore
from toolang.execution.types import ThreadKind, ThreadRef, valid_thread_id
from toolang.up import AgentCore


@pytest.mark.parametrize("kind", list(ThreadKind))
def test_thread_kind_is_derived_across_records_views_and_events(tmp_path, kind):
    path = tmp_path / "runs.db"
    identity = f"{kind.value}_example"
    with closing(RunStore(path)) as store:
        record, control = store.create_thread(thread_id=identity)
        info = ThreadInfo.from_records(record, head=control.ref, input_parts=())
        event = ThreadCreated(identity, control.ref, record.created_at)

        assert (
            ThreadRef(identity).kind == record.kind == info.kind == event.kind == kind
        )
        assert info.title == kind.value
        assert {item.name for item in fields(ThreadRecord)} == {
            "id",
            "created_at",
            "updated_at",
            "horizon",
        }
        assert "origin" not in record_to_data(record)
        assert "kind" not in record_to_data(record)
        payload = TypeAdapter(ThreadInfo).dump_python(info, mode="json")
        assert "kind" not in payload
        assert "origin" not in payload
        assert "channel" not in payload
        assert TypeAdapter(ThreadInfo).validate_python(payload).kind == kind
        assert replace(info, id="work_other").kind == ThreadKind.WORK
        assert event_from_data(event_to_data(event)) == event
        assert set(event_to_data(event)) == {"type", "thread", "control", "created_at"}

    with closing(RunStore(path, read_only=True)) as reopened:
        restored = reopened.get_thread(thread_id=identity)
        assert restored is not None and restored.kind == kind
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
    assert "origin" not in columns and "kind" not in columns


@pytest.mark.parametrize(
    "identity",
    [
        "chat",
        "chat_",
        "script_old",
        "term_old",
        "web_old",
        "worker_old",
        "spawn_old",
        "custom_example",
        "run_example",
        "sha256_example",
        "chat_bad.id",
    ],
)
def test_unknown_or_empty_thread_kind_is_rejected_before_writes(tmp_path, identity):
    assert not valid_thread_id(identity)
    with pytest.raises(ValueError, match="invalid thread ref"):
        ThreadRef(identity)
    with closing(RunStore(tmp_path / "runs.db")) as store:
        with pytest.raises(ValueError, match="invalid thread id"):
            store.create_thread(thread_id=identity)
        assert store.list_threads() == []
        assert store.list_controls() == ()


@pytest.mark.parametrize("kind", list(ThreadKind))
def test_pending_run_keeps_thread_active_in_summaries_and_filters(tmp_path, kind):
    identity = f"{kind.value}_pending"
    with closing(RunStore(tmp_path / "runs.db")) as store:
        store.create_thread(thread_id=identity)
        run, _ = accept_run(
            store,
            run_id="run_pending",
            parent=None,
            thread=identity,
            input=Message.user("Waiting to start"),
            context={},
            request_id=None,
            created_at="2026-10-20T00:00:00Z",
        )
        assert run.status == "pending"
        history = RunHistory(store)
        detail = history.get_thread(identity, run_limit=0)
        assert detail is not None and detail.status == "running"
        assert detail.active_run is not None
        assert detail.active_run.id == run.id
        assert detail.active_run.status == "pending"
        assert [item.id for item in history.list_threads(status="running")] == [
            identity
        ]
        assert history.list_threads(status="idle") == []


def test_thread_api_filters_by_kind_without_repeating_it_in_responses(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    core = AgentCore(layout)
    app = create_app(
        core, CapsManager(layout), ChoresManager(layout), cors_allowed_origins=()
    )
    try:
        identities = {kind: core.threads.create(kind=kind) for kind in ThreadKind}
        with TestClient(app) as client:
            for kind in ThreadKind:
                response = client.get("/api/v0/threads", params={"kind": kind.value})
                assert response.status_code == 200
                assert [item["id"] for item in response.json()] == [identities[kind]]
                assert "origin" not in response.json()[0]
                assert "kind" not in response.json()[0]
                assert "channel" not in response.json()[0]
                detail = client.get(f"/api/v0/threads/{identities[kind]}")
                assert detail.status_code == 200
                assert not {"origin", "kind", "channel"} & detail.json().keys()
            assert (
                client.get("/api/v0/threads", params={"kind": "script"}).status_code
                == 422
            )
        parameters = app.openapi()["paths"]["/api/v0/threads"]["get"]["parameters"]
        assert "channel" not in {item["name"] for item in parameters}
    finally:
        asyncio.run(core.close())
