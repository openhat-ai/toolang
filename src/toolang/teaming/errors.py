"""Errors exposed by teaming services."""

from toolang.common.errors import ToolangError


class TeamingError(ToolangError):
    """Invalid teaming configuration or operation."""


class MessagingError(TeamingError):
    """Invalid messaging identity, destination, or operation."""


class BackendUnavailable(MessagingError):
    """The backend connection failed; reads may reconnect."""


class LeaseLost(MessagingError):
    """The agent no longer owns the lease for this operation."""


class HubIdentityChanged(MessagingError):
    """The Hub no longer matches the client's fixed backend or human identity."""


class SendUnconfirmed(MessagingError):
    """The server may have accepted the message; do not retransmit automatically."""


class EventProtocolError(RuntimeError):
    """The established event dataset is inconsistent; repair is required."""


class EventRecoveryRequired(RuntimeError):
    """The source must publish a new structural generation."""


class ScopeUnavailable(ValueError):
    """A requested origin or retained scope does not exist."""


class ForgottenTree(ValueError):
    """Reattach at the previous checkpoint before applying this retry."""
