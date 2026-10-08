"""Errors exposed by teaming services."""

from toolang.common.errors import ToolangError


class TeamingError(ToolangError):
    """Invalid teaming configuration or operation."""


class MessagingError(TeamingError):
    """Invalid messaging identity, destination, or operation."""


class BackendUnavailable(MessagingError):
    """The backend connection failed; reads may reconnect."""


class SendUnconfirmed(MessagingError):
    """The server may have accepted the message; do not retransmit automatically."""
