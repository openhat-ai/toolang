"""Dependencies supplied only to the current-agent management tools."""

from dataclasses import dataclass
from typing import Literal

from toolang.base.types.tool import ToolContext
from toolang.common.layout import AgentLayout
from toolang.state.types import StateSync

HomeFileCategory = Literal["program", "config", "cap", "asset", "job"]


@dataclass(frozen=True, slots=True, kw_only=True)
class MeToolContext(ToolContext):
    layout: AgentLayout
    sync_state: StateSync | None = None


@dataclass(frozen=True, slots=True)
class HomeFile:
    key: str
    category: HomeFileCategory
