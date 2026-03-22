"""Shared Toolang concepts.

This package defines stable shared constructs used across Toolang runtime,
sync, caps, and storage code. It owns reusable concepts, not orchestration.
"""

from .caps import (
    CapContent,
    CapEntry,
    CapKind,
    CapParam,
    CapRef,
    CapSidecar,
)
from .execution import (
    ExecutionStrategy,
    Message,
    MessageOrigin,
    MessageSender,
    RuntimeLoop,
)
from .identity import AgentKind, AgentRef, AgentSelector, AgentUri
from .sandbox import (
    HOST_SANDBOX,
    SandboxRuntimeInfo,
    SandboxSpec,
    SandboxState,
)

__all__ = [
    "AgentKind",
    "AgentRef",
    "AgentSelector",
    "AgentUri",
    "CapContent",
    "CapEntry",
    "CapKind",
    "CapParam",
    "CapRef",
    "CapSidecar",
    "ExecutionStrategy",
    "HOST_SANDBOX",
    "Message",
    "MessageOrigin",
    "MessageSender",
    "RuntimeLoop",
    "SandboxRuntimeInfo",
    "SandboxSpec",
    "SandboxState",
]
