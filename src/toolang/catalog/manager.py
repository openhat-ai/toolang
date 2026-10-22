"""Process-local catalog collections for one agent layout."""

from __future__ import annotations

from toolang.common.layout import AgentLayout

from .cap import AuthoredCaps
from .config import ConfiguredCaps
from .chore import AuthoredChores


class CapsManager:
    """Group the root and agent-home capability catalogs."""

    __slots__ = (
        "home_authoring",
        "home_configured",
        "root_authoring",
        "root_configured",
    )

    def __init__(self, layout: AgentLayout) -> None:
        self.home_authoring = AuthoredCaps(layout.home, lock_path=layout.home_edit_lock)
        self.home_configured = ConfiguredCaps(
            layout.config, lock_path=layout.home_edit_lock
        )
        self.root_authoring = AuthoredCaps(
            layout.place, lock_path=layout.place_edit_lock
        )
        self.root_configured = ConfiguredCaps(
            layout.place_config, lock_path=layout.place_edit_lock
        )


class ChoresManager:
    """Group the authored chore catalogs for one agent layout."""

    __slots__ = ("home_authoring",)

    def __init__(self, layout: AgentLayout) -> None:
        self.home_authoring = AuthoredChores(
            layout.home,
            lock_path=layout.home_edit_lock,
            agent_name=layout.name,
        )
