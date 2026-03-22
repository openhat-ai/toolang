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
    parse_cap_body,
    parse_front_matter,
    ParsedCapBody,
    PromptFrontmatter,
    PsycheFrontmatter,
    ServiceFrontmatter,
    SkillFrontmatter,
)
from .channel import ChannelName, InboundDelivery, OutboundMessage, ReplyTarget
from .execution import (
    ExecutionStrategy,
    Message,
    MessageOrigin,
    MessageSender,
    RuntimeLoop,
)
from .identity import AgentKind, AgentRef, AgentSelector, AgentUri
from .layout import AgentHome, AgentRoom, ToolangRoot
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
    "AgentHome",
    "AgentRoom",
    "CapFrontmatter",
    "CapContent",
    "CapEntry",
    "CapKind",
    "CapParam",
    "CapRef",
    "CapSidecar",
    "ChannelName",
    "InboundDelivery",
    "OutboundMessage",
    "parse_cap_body",
    "parse_front_matter",
    "ParsedCapBody",
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
    "ReplyTarget",
    "ToolangRoot",
]
