"""Driver admission, effective chore definitions, and caller identity."""

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.support.execution_harness import ExecutionHarness, TEST_MODEL_REF
from toolang.api.app import create_app
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.catalog import CapsManager, ChoresManager
from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.common.ids import chore_id
from toolang.up.core import AgentCore
from toolang.driver.chore.store import ChoreStore
from toolang.catalog.chores import load_chores


def test_flat_authoring_and_inline_shadowing_with_chore_driver_disabled(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="chore cleanup:\n  Inline wins.\nagic:\n  {{_}}\n",
        responses=[],
    )
    layout = harness.setup.layout
    layout.program.write_text("chore cleanup:\n  Inline wins.\nagic:\n  {{_}}\n")
    harness.store.close()
    core = AgentCore(layout, drivers=frozenset())
    asyncio.run(core.state.refresh())
    root_file = AuthoredChores(layout.root).create(
        ChoreFile.parse("Root body.", name="cleanup")
    )
    home_file = AuthoredChores(layout.home).create(
        ChoreFile.parse("Home body.", name="cleanup")
    )
    app = create_app(
        core, CapsManager(layout), ChoresManager(layout), cors_allowed_origins=()
    )
    try:
        with TestClient(app) as client:
            items = client.get("/api/v1/chores").json()
            assert len(items) == 1
            assert items[0]["id"] == chore_id("cleanup")
            assert items[0]["ref"] == "chore/cleanup"
            assert "stage" not in items[0] and "kind" not in items[0]
            detail = client.get("/api/v1/chores/cleanup").json()
            assert detail["body"] == "Inline wins."
            assert client.post("/api/v1/chores/cleanup/run").status_code == 409
            created = client.post(
                "/api/v1/chores", json={"name": "once", "body": "Do this once."}
            )
            assert created.status_code == 201
            assert created.json()["schedule"] is None
            assert (layout.home / "chores" / "once.md").is_file()
            with_store = ChoreStore(layout.run_store)
            try:
                with_store.reconcile(chores=load_chores(layout))
                assert client.post("/api/v1/chores/once/cancel").status_code == 200
                record = with_store.get(chore_id=chore_id("once"))
                assert record is not None and record.status == "canceled"
                assert client.delete("/api/v1/chores/once").status_code == 204
                record = with_store.get(chore_id=chore_id("once"))
                assert record is not None and record.status == "canceled"
            finally:
                with_store.close()
            for path in (
                "tasks",
                "jobs",
                "chores/cleanup/archive",
                "chores/cleanup/draft",
                "chores/cleanup/ready",
                "chores/cleanup/reopen",
            ):
                assert client.post(f"/api/v1/{path}", json={}).status_code in {404, 405}
        assert root_file.path is not None and home_file.path is not None
        assert root_file.path.read_text() == "Root body."
        assert home_file.path.read_text() == "Home body."
    finally:
        asyncio.run(core.close())


def test_two_clients_share_chat_thread_and_controls_keep_both_identities(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat:\n  {{_}}\n",
        responses=[ModelCallResult(message=Message.assistant("done"))] * 2,
    )
    harness.store.close()
    core = AgentCore(harness.setup.layout, drivers=frozenset({"chat"}))
    core.setup = SimpleNamespace(current=lambda: harness.setup)
    core.state = SimpleNamespace(current=lambda: harness.state)
    app = create_app(
        core,
        CapsManager(core.layout),
        ChoresManager(core.layout),
        cors_allowed_origins=(),
    )
    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/v1/threads", json={"client_id": "web-1", "request_id": "create-1"}
            )
            thread = created.json()["thread"]["id"]
            for client_id, request_id in (("web-1", "run-1"), ("tui-2", "run-2")):
                result = client.post(
                    "/api/v1/chat/runs/authored/stream",
                    json={
                        "thread_id": thread,
                        "client_id": client_id,
                        "request_id": request_id,
                        "runnable": {"ref": "agic:chat", "input": {"_": "hello"}},
                        "model": {"ref": TEST_MODEL_REF},
                        "policy": {"allow": [], "limits": {}},
                    },
                )
                assert result.status_code == 200
                record = core.store.get_run_control(
                    run_id=result.headers["X-Toolang-Run-ID"], index=0
                )
                assert record is not None
                assert (record.client_id, record.request) == (client_id, request_id)
            assert len(core.store.list_threads()) == 1
            control = core.store.get_thread_control(thread_id=thread, index=0)
            assert control is not None
            assert (control.client_id, control.request) == ("web-1", "create-1")
    finally:
        asyncio.run(core.close())


def test_chore_inspection_reads_current_source_and_rejects_hidden_mutations(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path, source="chore cleanup:\n  Old inline.\nagic:\n  {{_}}\n", responses=[]
    )
    layout = harness.setup.layout
    harness.store.close()
    core = AgentCore(layout, drivers=frozenset())
    home = AuthoredChores(layout.home).create(
        ChoreFile.parse("Home body.", name="cleanup")
    )
    layout.program.write_text("chore cleanup:\n  New inline.\nflow ready:\n  pass\n")
    asyncio.run(core.state.refresh())
    app = create_app(
        core, CapsManager(layout), ChoresManager(layout), cors_allowed_origins=()
    )
    try:
        with TestClient(app) as client:
            endpoint = "/api/v1/chores/cleanup"
            assert client.get(endpoint).json()["body"] == "New inline."
            assert (
                client.patch(endpoint, json={"body": "Hidden edit."}).status_code == 409
            )
            assert client.delete(endpoint).status_code == 409
            assert home.path is not None
            assert home.path.read_text() == "Home body."
            assert (
                client.post(
                    "/api/v1/chores", json={"name": "cleanup", "body": "Hidden new."}
                ).status_code
                == 409
            )
    finally:
        asyncio.run(core.close())


def test_chore_api_edits_visible_home_file_and_preserves_root(tmp_path):
    harness = ExecutionHarness.create(tmp_path, source="agic:\n  {{_}}\n", responses=[])
    layout = harness.setup.layout
    harness.store.close()
    core = AgentCore(layout, drivers=frozenset())
    root_file = AuthoredChores(layout.root).create(
        ChoreFile.parse("Root body.", name="cleanup")
    )
    asyncio.run(core.state.refresh())
    app = create_app(
        core, CapsManager(layout), ChoresManager(layout), cors_allowed_origins=()
    )
    try:
        with TestClient(app) as client:
            endpoint = "/api/v1/chores/cleanup"
            assert (
                client.patch(endpoint, json={"body": "Hidden edit."}).status_code == 404
            )
            assert client.delete(endpoint).status_code == 404
            created = client.post(
                "/api/v1/chores", json={"name": "cleanup", "body": "Home body."}
            )
            assert created.status_code == 201
            assert client.get(endpoint).json() == created.json()
            updated = client.patch(endpoint, json={"body": "Edited home."})
            assert updated.status_code == 200
            assert client.get(endpoint).json() == updated.json()
            assert client.delete(endpoint).status_code == 204
            assert client.get(endpoint).status_code == 404
            assert (
                root_file.path is not None
                and root_file.path.read_text() == "Root body."
            )
    finally:
        asyncio.run(core.close())


def test_invalid_home_file_does_not_duplicate_accepted_inline_chore(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="chore cleanup:\n  Inline wins.\nflow chat():\n  pass\n",
        responses=[],
    )
    layout = harness.setup.layout
    layout.program.write_text("chore cleanup:\n  Inline wins.\nflow chat():\n  pass\n")
    harness.store.close()
    source = layout.home / "chores" / "cleanup.md"
    source.parent.mkdir()
    source.write_text("---\nschedule: invalid\n---\nBroken file.")
    core = AgentCore(layout, drivers=frozenset())
    state = asyncio.run(core.state.refresh())
    assert state.chores[0].line == 1
    assert any(
        file.key == "chores/cleanup.md" and not file.accepted for file in state.files
    )
    app = create_app(
        core, CapsManager(layout), ChoresManager(layout), cors_allowed_origins=()
    )
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/chores")
            assert response.status_code == 200
            assert len(response.json()) == 1
            assert response.json()[0]["path"] == "agent.too"
            assert response.json()[0]["source_error"] is None
            assert client.get("/api/v1/chores/cleanup").json()["body"] == "Inline wins."
    finally:
        asyncio.run(core.close())


def test_invalid_chore_name_does_not_block_api_inspection_or_sibling_deletion(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path, source="flow chat():\n  pass\n", responses=[]
    )
    layout = harness.setup.layout
    harness.store.close()
    directory = layout.home / "chores"
    directory.mkdir()
    (directory / " bad.md").write_text("Invalid name.")
    (directory / "good.md").write_text("Valid sibling.")
    core = AgentCore(layout, drivers=frozenset())
    asyncio.run(core.state.refresh())
    app = create_app(
        core, CapsManager(layout), ChoresManager(layout), cors_allowed_origins=()
    )
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/chores")
            assert response.status_code == 200
            entries = {entry["path"]: entry for entry in response.json()}
            assert entries["chores/ bad.md"]["id"] is None
            assert entries["chores/ bad.md"]["runtime"]["thread_id"] is None
            assert client.get("/api/v1/chores/%20bad").status_code == 200
            assert client.delete("/api/v1/chores/good").status_code == 204
            assert client.delete("/api/v1/chores/%20bad").status_code == 204
    finally:
        asyncio.run(core.close())
