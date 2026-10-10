"""Construct the supported driver from an already resolved configuration."""

from ..config import BackendConfig
from .protocol import Backend


def create_backend(config: BackendConfig) -> Backend:
    from .valkey import ValkeyBackend

    return ValkeyBackend(config)
