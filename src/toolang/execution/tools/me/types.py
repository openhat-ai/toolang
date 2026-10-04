"""Dependencies supplied only to the current-agent management tools."""

from dataclasses import dataclass
from typing import Literal

from toolang.base.types.tool import ToolContext
from toolang.common.layout import AgentLayout
from toolang.state.state import AgentState

HomeFileCategory = Literal["program", "config", "cap", "asset", "job"]


@dataclass(frozen=True, slots=True, kw_only=True)
class MeToolContext(ToolContext):
    layout: AgentLayout
    state: AgentState | None = None


@dataclass(frozen=True, slots=True)
class HomeFile:
    key: str
    category: HomeFileCategory
