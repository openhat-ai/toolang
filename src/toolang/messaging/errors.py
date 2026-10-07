"""Errors at the messaging boundary."""

from toolang.common.errors import ToolangError


class MessagingError(ToolangError):
    """An invalid destination, identity, or messaging operation."""


class SendUnconfirmed(MessagingError):
    """The server may have accepted the message; do not blindly retransmit."""
