"""Shared Toolang concepts.

This package defines stable shared constructs used across Toolang runtime,
sync, caps, and storage code. It owns reusable concepts, not orchestration.
"""

from .caps import (
    CapFrontmatter,
    CapContent,
    CapEntry,
    CapKind,
    CapParam,
    CapRef,
    CapSidecar,
    parse_front_matter,
    PromptFrontmatter,
    PsycheFrontmatter,
    ServiceFrontmatter,
    SkillFrontmatter,
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
    "CapFrontmatter",
    "CapContent",
    "CapEntry",
    "CapKind",
    "CapParam",
    "CapRef",
    "CapSidecar",
    "parse_front_matter",
    "ExecutionStrategy",
    "HOST_SANDBOX",
    "Message",
    "MessageOrigin",
    "MessageSender",
    "PromptFrontmatter",
    "PsycheFrontmatter",
    "RuntimeLoop",
    "ServiceFrontmatter",
    "SandboxRuntimeInfo",
    "SandboxSpec",
    "SandboxState",
    "SkillFrontmatter",
]
