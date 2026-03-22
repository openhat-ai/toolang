"""HTTP-facing agent API helpers."""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://too.run",
]
DEFAULT_AGENT_LINK_BASE = "https://too.run"


def add_cors(
    app: FastAPI,
    *,
    allow_origins: list[str] | None = None,
) -> None:
    """Install Toolang's default CORS policy on one FastAPI application."""

    origins = list(allow_origins or DEFAULT_CORS_ORIGINS)
    if not origins:
        return
    cors_middleware = cast(Any, CORSMiddleware)
    app.add_middleware(
        cors_middleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_private_network=True,
    )


def agent_link_for_port(port: int) -> str:
    """Return the human-facing agent page URL for one local port."""

    return f"{DEFAULT_AGENT_LINK_BASE.rstrip('/')}/{port}"


def agent_link_from_endpoint(endpoint: str | None) -> str | None:
    """Return a human-facing agent page URL derived from one local endpoint."""

    if endpoint is None or not endpoint.strip():
        return None
    try:
        port = urlsplit(endpoint).port
    except ValueError:
        return None
    if port is None:
        return None
    return agent_link_for_port(port)
