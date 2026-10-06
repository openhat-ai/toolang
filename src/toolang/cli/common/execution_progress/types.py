"""Terminal-independent execution progress vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from toolang.execution.events import StepBegin
from toolang.execution.types import ToolStepGiven
from toolang.lang.ast import FlowStmt

ProgressTone = Literal["progress", "normal", "active", "error", "warning"]
ProgressFormat = Literal["plain", "markdown"]
ProgressSurface = Literal["none", "tool_summary", "tool_error"]
ProgressLeader = Literal["none", "run", "handoff", "iteration"]


@dataclass(frozen=True, slots=True)
class StepOperation:
    """Presentation meaning of a Step, retaining its original event and source."""

    begin: StepBegin
    name: str
    source: Literal["flow", "model", "tool"]
    statement: FlowStmt | None = None
    tool: ToolStepGiven | None = None
    runnable: str = ""
    run_scope: bool = False
    tool_marker: Literal["›", "✧"] = "›"

    @property
    def is_flow(self) -> bool:
        return self.statement is not None and not self.run_scope

    @property
    def timed(self) -> bool:
        return self.source == "tool" and self.name == "compact"


@dataclass(frozen=True, slots=True)
class ProgressRow:
    """One semantic progress row before surface-specific styling."""

    text: str
    tone: ProgressTone = "progress"
    wrap_live: bool = False
    format: ProgressFormat = "plain"
    prefix: str = ""
    gap_before: bool = False
    surface: ProgressSurface = "none"
    right_text: str = ""
    leader: ProgressLeader = "none"
    facts: tuple[str, ...] = ()
    right_status: str = ""
    right_identity: str = ""


@dataclass(frozen=True, slots=True)
class ProgressBlock:
    """One progress fragment with an explicit inter-section leading gap."""

    key: str
    rows: tuple[ProgressRow, ...]
    gap_before: bool = False


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """Newly committed fragments plus the complete current live snapshot."""

    committed: tuple[ProgressBlock, ...] = ()
    live: tuple[ProgressBlock, ...] = ()
