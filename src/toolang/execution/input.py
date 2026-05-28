"""Compatibility facade for run input assembly."""

from __future__ import annotations

from .assembly import RunInput
from .binding import RunBinding, allocate_run_id, bind_run_request
from .effective import effective_origin_model_selectors, select_origin_thunk

__all__ = [
    "RunBinding",
    "RunInput",
    "allocate_run_id",
    "bind_run_request",
    "effective_origin_model_selectors",
    "select_origin_thunk",
]
