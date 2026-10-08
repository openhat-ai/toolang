"""Resolve root teaming configuration for human messaging clients."""

from pathlib import Path

from toolang.teaming.config import BackendConfig
from toolang.setup.teaming import load_teaming_root


def settings(root: Path) -> tuple[BackendConfig, str]:
    config = load_teaming_root(root)
    return config.backend, config.human
