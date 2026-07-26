"""Process-local catalog collections for one agent layout."""

from __future__ import annotations

from toolang.common.layout import AgentLayout

from .cap import AuthoredCaps
from .config import WiredCaps
from .job import AuthoredJobs


class CapsManager:
    """Group the root and agent-home capability catalogs."""

    __slots__ = (
        "home_authoring",
        "home_wiring",
        "root_authoring",
        "root_wiring",
    )

    def __init__(self, layout: AgentLayout) -> None:
        self.home_authoring = AuthoredCaps(layout.home)
        self.home_wiring = WiredCaps(layout.config)
        self.root_authoring = AuthoredCaps(layout.root)
        self.root_wiring = WiredCaps(layout.root_config)


class JobsManager:
    """Group the authored job catalogs for one agent layout."""

    __slots__ = ("home_authoring",)

    def __init__(self, layout: AgentLayout) -> None:
        self.home_authoring = AuthoredJobs(layout.home)
