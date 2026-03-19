from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


def add_cors(
    app: FastAPI,
    *,
    allow_origins: list[str] | None = None,
) -> None:
    origins = list(allow_origins or DEFAULT_CORS_ORIGINS)
    if not origins:
        return
    cors_middleware = cast(Any, CORSMiddleware)
    app.add_middleware(
        cors_middleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
