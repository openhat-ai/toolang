"""Consume the root messaging setup for CLI commands."""

from pathlib import Path

from toolang.messaging.config import MessagingConfig
from toolang.messaging.errors import MessagingError
from toolang.setup.messaging import load_messaging_setup


def settings(root: Path) -> tuple[MessagingConfig, str]:
    setup = load_messaging_setup(root)
    if setup.config is None:
        raise MessagingError("Messaging is disabled in the root configuration")
    return setup.config, setup.human
