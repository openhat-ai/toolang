"""Setup-owned configuration and environment loading."""

from __future__ import annotations

import os
from pathlib import Path
import tomllib

from dotenv import dotenv_values
from toolang.common.layout import AgentLayout


def load_setup_config(layout: AgentLayout) -> dict[str, object]:
    """Load the root-scoped setup configuration."""

    return _load_toml(layout.root_config)


def load_setup_envs(layout: AgentLayout) -> dict[str, str]:
    """Load root dotenv defaults below the process environment."""

    envs = _load_dotenv(layout.root_env)
    envs.update(os.environ)
    return envs


def _load_toml(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return {
        key: value
        for key, value in dotenv_values(path).items()
        if isinstance(key, str) and isinstance(value, str)
    }
