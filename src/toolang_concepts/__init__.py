"""Shared Toolang concepts.

This package defines stable shared constructs used across Toolang runtime,
sync, caps, and storage code. It owns reusable concepts, not orchestration.
"""

from .caps import (
    CAP_KINDS,
    TEXT_CAP_KINDS,
    CapContent,
    CapEntry,
    CapKind,
    CapParam,
    CapRef,
    CapSidecar,
    InlineCapKind,
    refs_attr_name,
    section_name,
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
    "CAP_KINDS",
    "TEXT_CAP_KINDS",
    "CapContent",
    "CapEntry",
    "CapKind",
    "CapParam",
    "CapRef",
    "CapSidecar",
    "ExecutionStrategy",
    "HOST_SANDBOX",
    "InlineCapKind",
    "Message",
    "MessageOrigin",
    "MessageSender",
    "RuntimeLoop",
    "SandboxRuntimeInfo",
    "SandboxSpec",
    "SandboxState",
    "refs_attr_name",
    "section_name",
]
