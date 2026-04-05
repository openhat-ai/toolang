"""Runtime server package."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import create_agent_app, run_agent

__all__ = ["create_agent_app", "run_agent"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from .app import create_agent_app, run_agent

    exports = {
        "create_agent_app": create_agent_app,
        "run_agent": run_agent,
    }
    return exports[name]
