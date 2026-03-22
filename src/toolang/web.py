"""Small shared web helpers used by FastAPI applications."""

from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://too.run",
]


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
