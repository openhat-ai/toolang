from fastapi import FastAPI
from fastapi.testclient import TestClient

from toolang.api.routers.stats import router

from .conftest import DAY, NOW


def client_for(service):
    app = FastAPI()
    app.state.stats = service
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_public_endpoints_serialize_quantities_models_and_daily_dates(fixture):
    root = fixture.root("api", NOW - 4 * DAY)
    fixture.call(root, NOW - 3 * DAY, NOW - 3 * DAY + 1)
    fixture.finish(root, NOW - 2 * DAY)
    assert fixture.refresh() is None
    with client_for(fixture.stats) as client:
        response = client.get("/api/v1/stats/summary", params={"root_run_id": root})
        assert response.status_code == 200
        result = response.json()
        assert len(result["data"]) == 8
        assert result["data"][0]["in"] == 100 and result["data"][0]["spend"] == 0.1
        assert result["data"][0]["root_runs"] == 1
        assert result["snapshot"]["computed_at"]
        models = client.get("/api/v1/stats/models", params={"window": "all"}).json()[
            "data"
        ]
        assert models[0]["model_ref"] == "test/model" and models[0]["root_runs"] is None
        daily = client.get(
            "/api/v1/stats/daily",
            params={
                "start_date": "2026-10-14",
                "end_date": "2026-10-19",
                "timezone": "Asia/Shanghai",
            },
        )
        assert daily.status_code == 200
        assert len(daily.json()["data"]) == 5
        assert daily.json()["data"][-1]["partial"]


def test_scope_and_range_errors_have_stable_http_statuses(fixture):
    root = fixture.root("scope", NOW - DAY)
    ref = fixture.call(root, NOW - 10, NOW - 9, tool=True)
    child = fixture.root("child", NOW - 5, parent=ref, thread="chat_stats")
    with client_for(fixture.stats) as client:
        for params in (
            {"root_run_id": "bad"},
            {"thread_id": "chat_stats", "root_run_id": root},
            {"root_run_id": child},
        ):
            assert client.get("/api/v1/stats/summary", params=params).status_code == 422
        for params in ({"root_run_id": "run_missing"}, {"thread_id": "chat_missing"}):
            assert client.get("/api/v1/stats/summary", params=params).status_code == 404
        assert (
            client.get("/api/v1/stats/models", params={"window": "2h"}).status_code
            == 422
        )
        for params in (
            {"start_date": "2026-10-18", "end_date": "2026-10-18", "timezone": "UTC"},
            {
                "start_date": "2026-10-18",
                "end_date": "2026-10-19",
                "timezone": "invalid",
            },
            {"start_date": "2026-10-18", "end_date": "2026-10-20", "timezone": "UTC"},
        ):
            assert client.get("/api/v1/stats/daily", params=params).status_code == 422


def test_unavailable_statistics_returns_503(fixture):
    fixture.stats.close()
    with client_for(fixture.stats) as client:
        assert client.get("/api/v1/stats/summary").status_code == 503
    with client_for(None) as client:
        assert client.get("/api/v1/stats/models").status_code == 503
