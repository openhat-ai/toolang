"""Shared agent loop protocols."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from ..types.message import Message
from ..types.model import ModelTarget
from ..types.run import ModelCallResult, RunResult, ToolCall, ToolCallResult
from ..protocols.tool import AgentTool


@runtime_checkable
class RunContext(Protocol):
    """Minimal run context contract exposed to agent loops."""

    @property
    def instructions(self) -> str:
        """Return the provider-neutral instruction text."""

    @property
    def messages(self) -> tuple[Message, ...]:
        """Return the current conversation messages."""

    @property
    def model(self) -> ModelTarget:
        """Return the resolved model target for this run."""

    @property
    def tools(self) -> Mapping[str, AgentTool]:
        """Return the available tool mapping for this run."""

    def call_model(self) -> ModelCallResult:
        """Perform one model call and update run state."""

    def call_tool(self, call: ToolCall) -> ToolCallResult:
        """Perform one tool call and update run state."""

    def call_tools(self, calls: Sequence[ToolCall]) -> tuple[ToolCallResult, ...]:
        """Perform multiple tool calls and update run state."""

    def has_pending_inputs(self) -> bool:
        """Return whether unconsumed client inputs are waiting for this run."""

    def finish(self) -> RunResult:
        """Finalize one run result from accumulated state."""


@runtime_checkable
class AgentLoop(Protocol):
    """Minimal agent loop contract."""

    name: str

    def run(self, context: RunContext) -> RunResult:
        """Run one bound execution context."""
