"""Discover the running root Hub for human messaging clients."""

from pathlib import Path

from toolang.teaming.schemas import HubConnection
from toolang.up.hub import HubProcess


def settings(root: Path) -> tuple[HubConnection, str]:
    config = HubProcess(root).connection()
    return config, config.human
