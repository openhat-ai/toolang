"""Cap template routes expose one bundled default per kind."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from toolang.api.routers.caps import router
from toolang.catalog import templates


@pytest.mark.parametrize(
    ("collection", "kind"),
    [
        ("psyches", "psyche"),
        ("skills", "skill"),
        ("services", "service"),
        ("prompts", "prompt"),
    ],
)
def test_template_routes_expose_only_the_default(collection, kind):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    template = templates.load_template(kind)
    expected = {
        "kind": kind,
        "name": "default",
        "title": "Default",
        "description": template.description,
        "path": template.path,
    }
    with TestClient(app) as client:
        listing = client.get(f"/api/v1/{collection}/templates")
        assert listing.status_code == 200
        assert listing.json() == [expected]
        detail = client.get(f"/api/v1/{collection}/templates/default")
        assert detail.status_code == 200
        assert detail.json() == {**expected, "content": template.raw_text}
        for name in ("stdio", "missing"):
            assert (
                client.get(f"/api/v1/{collection}/templates/{name}").status_code == 404
            )
