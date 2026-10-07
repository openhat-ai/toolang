"""Resolve CLI messaging configuration at the invocation boundary."""

import getpass
from pathlib import Path

from toolang.cli.config import load_config
from toolang.messaging.config import MessagingConfig, human_name
from toolang.messaging.errors import MessagingError


def settings(root: Path) -> tuple[MessagingConfig, str]:
    config = load_config(root / "config.toml")
    messaging = MessagingConfig.from_config(config)
    if messaging is None:
        raise MessagingError("Set [messaging].url in the root config.toml first")
    return messaging, human_name(config, default=getpass.getuser())
