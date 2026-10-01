"""Capability mutations return the exact published State representation."""

import asyncio

from fastapi.testclient import TestClient
import pytest

from toolang.api.app import create_app
from toolang.catalog import CapsManager, JobsManager
from toolang.common.layout import AgentLayout
from toolang.up import AgentCore


@pytest.mark.parametrize("description", ["", "---\ndescription: A note\n---\n"])
@pytest.mark.parametrize("scope", ["home", "root"])
def test_authored_write_returns_published_content(tmp_path, description, scope):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    core = AgentCore(layout)
    asyncio.run(core.state.refresh())
    before = core.state.current()
    content = description + "New body.\n"
    try:
        with TestClient(
            create_app(core, CapsManager(layout), JobsManager(layout)),
            raise_server_exceptions=False,
        ) as client:
            response = client.put(
                "/api/v1/psyches/note/authored",
                json={"scope": scope, "content": content},
            )
            assert response.status_code == 200, response.text
            assert response.json()["content"] == content
            assert response.json()["scope"] == scope
            assert core.state.current().revision != before.revision
            detail = client.get("/api/v1/psyches/note")
            assert detail.json()["content"] == content
    finally:
        asyncio.run(core.close())


def test_root_write_returns_root_snapshot_when_shadowed(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    (layout.home / "psyches").mkdir(parents=True)
    (layout.home / "psyches" / "note.md").write_text("Home body.\n")
    core = AgentCore(layout)
    asyncio.run(core.state.refresh())
    try:
        with TestClient(
            create_app(core, CapsManager(layout), JobsManager(layout)),
            raise_server_exceptions=False,
        ) as client:
            response = client.put(
                "/api/v1/psyches/note/authored",
                json={"scope": "root", "content": "Root body.\n"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["content"] == "Root body.\n"
            assert (
                client.get("/api/v1/psyches/note").json()["content"] == "Home body.\n"
            )
    finally:
        asyncio.run(core.close())


def test_rejected_write_publication_does_not_return_previous_content(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    (layout.home / "psyches").mkdir(parents=True)
    (layout.home / "psyches" / "note.md").write_text("Old body.\n")
    core = AgentCore(layout)
    asyncio.run(core.state.refresh())
    before = core.state.current()
    layout.program.write_text("not a valid program ???")
    try:
        with TestClient(
            create_app(core, CapsManager(layout), JobsManager(layout)),
            raise_server_exceptions=False,
        ) as client:
            response = client.put(
                "/api/v1/psyches/note/authored", json={"content": "New body.\n"}
            )
            assert response.status_code == 409, response.text
            assert "saved" in response.json()["detail"]
            assert core.state.current() is before
            assert (layout.home / "psyches" / "note.md").read_text() == "New body.\n"
    finally:
        asyncio.run(core.close())


@pytest.mark.parametrize("scope", ["home", "root"])
def test_configured_write_materializes_content_and_delete_publishes(
    tmp_path, monkeypatch, scope
):
    from toolang.state import state as cap_state

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    core = AgentCore(layout)
    asyncio.run(core.state.refresh())
    monkeypatch.setattr(cap_state, "_github_repo_default_branch", lambda *_: "main")
    monkeypatch.setattr(cap_state, "_github_remote_exists", lambda *_: True)
    monkeypatch.setattr(
        cap_state,
        "_remote_materialized_files",
        lambda *, relative_entry_path, **kwargs: {
            str(relative_entry_path): b"Remote body.\n"
        },
    )
    try:
        with TestClient(
            create_app(core, CapsManager(layout), JobsManager(layout))
        ) as client:
            response = client.put(
                "/api/v1/prompts/rewrite/configured",
                json={"scope": scope, "ref": "acme/rewrite"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["content"] == "Remote body.\n"
            assert response.json()["form"] == "configured"
            response = client.delete(
                "/api/v1/prompts/rewrite/configured", params={"scope": scope}
            )
            assert response.status_code == 204
            assert client.get("/api/v1/prompts/rewrite").status_code == 404
    finally:
        asyncio.run(core.close())


def test_allow_excluded_authored_write_returns_snapshot_and_delete_publishes(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.config.write_text("[allow]\npsyches = []\n")
    core = AgentCore(layout)
    asyncio.run(core.state.refresh())
    try:
        with TestClient(
            create_app(core, CapsManager(layout), JobsManager(layout))
        ) as client:
            response = client.put(
                "/api/v1/psyches/note/authored", json={"content": "Body.\n"}
            )
            assert response.status_code == 200
            assert response.json()["content"] == "Body.\n"
            assert client.get("/api/v1/psyches/note").status_code == 404
            before = core.state.current().revision
            assert client.delete("/api/v1/psyches/note/authored").status_code == 204
            assert core.state.current().revision != before
            assert "psyche:note" not in core.state.current().caps
    finally:
        asyncio.run(core.close())
