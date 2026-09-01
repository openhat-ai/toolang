"""Agent process startup, lifecycle, and API server assembly."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import AgentCore


def __getattr__(name: str) -> Any:
    if name == "AgentCore":
        from .core import AgentCore

        globals()[name] = AgentCore
        return AgentCore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["AgentCore"]
