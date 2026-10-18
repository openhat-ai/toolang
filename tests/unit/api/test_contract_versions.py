import asyncio

from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
import pytest

from toolang.api.version import AGENT_API_VERSION
from toolang.api import app as app_module
from toolang.api.app import create_app
from toolang.api.schemas import RuntimeLifecyclePayload
from toolang.catalog import CapsManager, ChoresManager
from toolang.common.layout import AgentLayout
from toolang.execution.schemas import HistoryToolCursor
from toolang.up import AgentCore


def test_stats_routes_have_an_independent_version(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "STATS_API_VERSION", AGENT_API_VERSION + 7)
    layout = AgentLayout.resident(tmp_path, "alice")
    core = AgentCore(layout)
    try:
        app = create_app(core, CapsManager(layout), ChoresManager(layout))
        with TestClient(app) as client:
            assert client.get(f"/api/v{AGENT_API_VERSION}/threads").json() == []
            # A registered stats route reports unavailable service, rather than 404.
            assert (
                client.get(f"/api/v{AGENT_API_VERSION + 7}/stats/summary").status_code
                == 503
            )
            assert (
                client.get(f"/api/v{AGENT_API_VERSION}/stats/summary").status_code
                == 404
            )
            assert client.get("/api/v1/threads").status_code == 404
    finally:
        asyncio.run(core.close())


@pytest.mark.parametrize(
    "version", [AGENT_API_VERSION - 1, AGENT_API_VERSION + 1, True]
)
def test_cursor_uses_agent_api_version(version):
    adapter = TypeAdapter(HistoryToolCursor)
    current = HistoryToolCursor("/store", "read_runs", "cursor")
    data = adapter.dump_python(current)
    assert data["version"] == AGENT_API_VERSION
    assert adapter.validate_python(data) == current
    with pytest.raises(ValidationError):
        adapter.validate_python(data | {"version": version})


def test_lifecycle_uses_agent_api_version_and_rejects_retired_field():
    payload = RuntimeLifecyclePayload(launch_id="launch", lifetime="automatic")
    assert payload.api_version == AGENT_API_VERSION
    with pytest.raises(ValidationError):
        RuntimeLifecyclePayload.model_validate(
            {"launch_id": "launch", "lifetime": "automatic", "lifecycle_protocol": 1}
        )
